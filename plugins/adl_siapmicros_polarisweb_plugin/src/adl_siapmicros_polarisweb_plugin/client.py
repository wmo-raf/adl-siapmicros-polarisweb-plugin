import logging
from datetime import datetime

import requests
from django.core.cache import cache

logger = logging.getLogger(__name__)

DATE_FORMAT = "%Y%m%d%H%M"
DATA_TYPE = "val_data"

DEFAULT_TIMEOUT = 30

API_ROOT_PATH = "/api/polaris/"
STATIONS_PATH = "stations"
BASE_MEASURES_PATH = "base_measures"

# The ingestion diagnostic's shared HTTP status table. The category strings are
# written out rather than imported from core: an import of core's vocabulary
# would break this plugin at import time on an older core, and core drops any
# value it does not recognise anyway.
#
# 400 and 422 decline because a malformed request is our bug, 429 because rate
# limiting is our polling schedule, and 3xx because a redirect says nothing
# about the source. Nothing here ever stamps UNKNOWN: declining leaves core's
# read-time classification free to do better later, and a stamp does not.
STATUS_CATEGORIES = {
    401: "AUTH_FAILED",
    403: "PERMISSION_DENIED",
    404: "PATH_NOT_FOUND",
}


def category_for_status(status_code):
    """The diagnostic failure category for an HTTP status, or None when the
    status carries no honest one."""
    if status_code in STATUS_CATEGORIES:
        return STATUS_CATEGORIES[status_code]
    if status_code is not None and 500 <= status_code < 600:
        return "PROTOCOL_ERROR"
    return None


def _raise_for_status(response):
    """``raise_for_status()``, tagging the raised error for the diagnostic.

    The exception is stamped in place rather than wrapped, so the original
    type still matches core's own exception table and the traceback survives.
    A code from the server is proof the server answered, which is what makes
    every category derived from one layer 5.
    """
    try:
        response.raise_for_status()
    except requests.HTTPError as e:
        category = category_for_status(e.response.status_code)
        if category:
            e.adl_category = category
            e.adl_layer = 5
        raise


def _parsed_body(response):
    """The response body, as the JSON object this API answers with.

    A 2xx is not proof of an API response: ``requests`` follows redirects, so
    an expired session that lands on an HTML login page arrives here as a
    clean 200. Both that and a body of the wrong shape raise ``ValueError`` —
    requests' own ``JSONDecodeError`` is one too — so a caller has a single
    type to catch for "answered, but not with what we asked for".
    """
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("The response body was not a JSON object.")
    return payload


def _list_from(payload, key):
    """The list under ``key``, from a body that really carries one."""
    value = payload.get(key)
    if not isinstance(value, list):
        raise ValueError(f"The response carried no '{key}' list.")
    return value


class PolarisWebAPIClient:
    def __init__(self, api_token, host, timeout=DEFAULT_TIMEOUT, retries=None, use_cache=True):
        host = host.rstrip('/')
        self.api_token = api_token
        self.base_url = f"{host}{API_ROOT_PATH}"
        self.timeout = timeout
        self.use_cache = use_cache

        self.session = requests.Session()
        if retries is not None:
            # Mounted only when asked for. requests' default adapter already
            # retries nothing, so the ingestion path keeps its behaviour and
            # the on-demand diagnostic checks can say so explicitly.
            adapter = requests.adapters.HTTPAdapter(max_retries=retries)
            self.session.mount("https://", adapter)
            self.session.mount("http://", adapter)

    def _get(self, path, **kwargs):
        params = kwargs.pop('params', {})
        params['api_token'] = self.api_token
        kwargs.setdefault('timeout', self.timeout)
        url = f"{self.base_url}{path.lstrip('/')}"
        response = self.session.get(url, params=params, **kwargs)
        _raise_for_status(response)
        return _parsed_body(response)

    def _post(self, path, json=None, **kwargs):
        params = kwargs.pop('params', {})
        params['api_token'] = self.api_token
        kwargs.setdefault('timeout', self.timeout)
        url = f"{self.base_url}{path.lstrip('/')}"
        response = self.session.post(url, json=json, params=params, **kwargs)
        _raise_for_status(response)
        return _parsed_body(response)

    def get_stations(self):
        cache_key = f"{self.api_token}-polaris-stations"
        if self.use_cache:
            cached = cache.get(cache_key)
            if cached is not None:
                return cached

        data = self._get(f'/{STATIONS_PATH}', params={'limit': -1})
        stations = _list_from(data, 'items')

        stations_by_id = {str(s['id']): s for s in stations}

        if self.use_cache:
            cache.set(cache_key, stations_by_id, 86400)

        return stations_by_id

    def get_base_measures(self):
        cache_key = f"{self.api_token}-polaris-base-measures"
        if self.use_cache:
            cached = cache.get(cache_key)
            if cached is not None:
                return cached

        data = self._get(f'/{BASE_MEASURES_PATH}', params={'limit': -1})
        measures = _list_from(data, 'items')

        measures_by_id = {str(m['id']): m for m in measures}

        if self.use_cache:
            cache.set(cache_key, measures_by_id, 86400)

        return measures_by_id

    def get_measurements(self, station_id, measure_ids, start_date, end_date):
        """
        Fetch measurements for a single station and a list of measure IDs.

        :param station_id: int or str — Polaris station ID
        :param measure_ids: list of int/str — Polaris base_measure IDs to fetch
        :param start_date: datetime — start of the collection window
        :param end_date: datetime — end of the collection window
        :return: ``(records, sources_count)``, the records as dicts like
            {"observation_time": datetime, "<measure_id>": float, ...} and the
            count of entries the response carried — read before the value and
            timestamp handling below drops anything, so that a fault of ours
            can never read as the source having offered nothing. The request
            carries the window, so the source has already applied it. The
            count leaves the client by return value because the station link
            it is reported on belongs to the plugin, not here.
        """
        measures_map = {
            f"{station_id}_{measure_id}": DATA_TYPE
            for measure_id in measure_ids
        }

        body = {
            "date_start": start_date.strftime(DATE_FORMAT),
            "date_end": end_date.strftime(DATE_FORMAT),
            "measures": measures_map,
        }

        data = self._get('/data/series', json=body)
        series_list = _list_from(data, 'series')

        # Accumulate all values keyed by timestamp
        records_by_time = {}
        sources_count = 0

        for series in series_list:
            measure_id = str(series.get('measure_id'))
            values = series.get('data', {})

            # Counted as carried, before the value and timestamp handling
            # below drops anything: those drops are ours, and a source that
            # answered with entries did offer data.
            sources_count += len(values) if isinstance(values, (dict, list)) else 0

            for timestamp_str, raw_value in values.items():
                if raw_value is None:
                    continue

                try:
                    value = float(raw_value)
                except (TypeError, ValueError):
                    logger.warning(
                        "Could not parse value %r for measure %s at %s",
                        raw_value, measure_id, timestamp_str,
                    )
                    continue

                if timestamp_str not in records_by_time:
                    try:
                        observation_time = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M")
                    except ValueError:
                        logger.warning("Could not parse timestamp %r", timestamp_str)
                        continue
                    records_by_time[timestamp_str] = {"observation_time": observation_time}

                records_by_time[timestamp_str][measure_id] = value

        return list(records_by_time.values()), sources_count

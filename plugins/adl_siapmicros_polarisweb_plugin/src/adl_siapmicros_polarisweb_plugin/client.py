import logging
from datetime import datetime

import requests
from django.core.cache import cache

logger = logging.getLogger(__name__)

DATE_FORMAT = "%Y%m%d%H%M"
DATA_TYPE = "val_data"


class PolarisWebAPIClient:
    def __init__(self, api_token, host, timeout=30, use_cache=True):
        host = host.rstrip('/')
        self.api_token = api_token
        self.base_url = f"{host}/api/polaris/"
        self.timeout = timeout
        self.use_cache = use_cache
    
    def _get(self, path, **kwargs):
        params = kwargs.pop('params', {})
        params['api_token'] = self.api_token
        kwargs.setdefault('timeout', self.timeout)
        url = f"{self.base_url}{path.lstrip('/')}"
        response = requests.get(url, params=params, **kwargs)
        response.raise_for_status()
        return response.json()
    
    def _post(self, path, json=None, **kwargs):
        params = kwargs.pop('params', {})
        params['api_token'] = self.api_token
        kwargs.setdefault('timeout', self.timeout)
        url = f"{self.base_url}{path.lstrip('/')}"
        response = requests.post(url, json=json, params=params, **kwargs)
        response.raise_for_status()
        return response.json()
    
    def get_stations(self):
        cache_key = f"{self.api_token}-polaris-stations"
        if self.use_cache:
            cached = cache.get(cache_key)
            if cached is not None:
                return cached
        
        data = self._get('/stations', params={'limit': -1})
        stations = data.get('items', [])
        
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
        
        data = self._get('/base_measures', params={'limit': -1})
        measures = data.get('items', [])
        
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
        :return: list of dicts like {"observation_time": datetime, "<measure_id>": float, ...}
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
        series_list = data.get('series', [])
        
        # Accumulate all values keyed by timestamp
        records_by_time = {}
        
        for series in series_list:
            measure_id = str(series.get('measure_id'))
            values = series.get('data', {})
            
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
        
        return list(records_by_time.values())

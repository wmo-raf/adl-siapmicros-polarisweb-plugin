"""
Tests for the ingestion-diagnostic contracts: ``get_source_endpoint()``,
``check_source()``, ``check_station_source()``, the ``adl_sources_count``
duck-typed handover and the exception stamping in ``client.py``. See the
"Ingestion Diagnostic Contracts" page in the ADL developer guide.

All tests run without touching the database: model instances are built
unsaved and the HTTP layer is stubbed, so the seam under test is exactly the
contract core consumes. That is what ``SimpleTestCase`` buys here — Django
still calls ``setup_databases()`` whatever the class, so the suite is run on
this plugin's own compose stack with ``make test`` from the repo root.
"""

import ast
import os
from datetime import datetime, timezone
from unittest import mock

import requests
from adl.core.models import Network, Station
from adl.core.source_checks import SourceCheckResult, SourceCheckStatus
from django.test import SimpleTestCase

from adl_siapmicros_polarisweb_plugin.client import PolarisWebAPIClient, category_for_status
from adl_siapmicros_polarisweb_plugin.models import PolarisWebConnection, PolarisWebStationLink
from adl_siapmicros_polarisweb_plugin.plugins import PolarisWebPlugin

NOT_JSON = object()

HOST = "http://102.218.136.213:88"

START = datetime(2026, 8, 1, tzinfo=timezone.utc)
END = datetime(2026, 8, 2, tzinfo=timezone.utc)


class FakeResponse:
    """A stubbed ``requests`` response: status code, and a body that either
    parses or does not."""

    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.payload = payload

    def json(self):
        if self.payload is NOT_JSON:
            # What an HTML login page reached through a redirect looks like
            # from here. requests' own JSONDecodeError is a ValueError too.
            raise requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0)
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error", response=self)


class FakeAPIClient:
    """A stubbed Polaris client that answers the one call a check makes."""

    def __init__(self, stations=None, measures=None, error=None, measurements=None):
        self.stations = stations if stations is not None else {}
        self.measures = measures if measures is not None else {}
        self.error = error
        self.measurements = measurements

    def get_stations(self):
        if self.error is not None:
            raise self.error
        return self.stations

    def get_base_measures(self):
        if self.error is not None:
            raise self.error
        return self.measures

    def get_measurements(self, station_id, measure_ids, start_date, end_date):
        if self.error is not None:
            raise self.error
        return self.measurements


def make_connection(**kwargs):
    kwargs.setdefault("api_token", "token")
    kwargs.setdefault("host", HOST)
    return PolarisWebConnection(**kwargs)


def make_station_link(connection=None, mappings=(), **kwargs):
    kwargs.setdefault("polaris_station_id", 42)
    link = PolarisWebStationLink(**kwargs)
    link.network_connection = connection or make_connection()
    # Mappings live on the connection, and reading them would hit the database.
    link.get_variable_mappings = lambda: list(mappings)
    return link


def stub_api_client(client):
    """Patch the client factory, capturing the arguments the check passed."""
    calls = []

    def factory(self, **kwargs):
        calls.append(kwargs)
        return client

    patcher = mock.patch.object(PolarisWebConnection, "get_api_client", autospec=True,
                                side_effect=factory)
    return patcher, calls


def mapping(measure_id):
    return mock.Mock(polaris_measure_id=measure_id)


class GetApiClientTests(SimpleTestCase):
    """The factory's defaults are the ingestion path's behaviour, unchanged;
    only the on-demand checks ask for anything else."""

    def test_defaults_are_todays_ingestion_behaviour(self):
        client = make_connection().get_api_client()
        self.assertTrue(client.use_cache)
        self.assertEqual(client.timeout, 30)

    def test_checks_can_bound_and_bypass(self):
        client = make_connection().get_api_client(use_cache=False, timeout=5, retries=0)
        self.assertFalse(client.use_cache)
        self.assertEqual(client.timeout, 5)


class GetSourceEndpointTests(SimpleTestCase):

    def test_honours_the_explicit_port(self):
        # The field's own help text gives an IP literal on port 88, so this is
        # the ordinary case here rather than the exotic one.
        self.assertEqual(make_connection().get_source_endpoint(), ("102.218.136.213", 88))

    def test_falls_back_to_443_for_https_without_a_port(self):
        connection = make_connection(host="https://polaris.example.test")
        self.assertEqual(connection.get_source_endpoint(), ("polaris.example.test", 443))

    def test_falls_back_to_80_for_http_without_a_port(self):
        connection = make_connection(host="http://polaris.example.test")
        self.assertEqual(connection.get_source_endpoint(), ("polaris.example.test", 80))


class CheckSourceTests(SimpleTestCase):

    def check(self, connection):
        result = connection.check_source()
        self.assertIsInstance(result, SourceCheckResult)
        self.assertIn(result.status, SourceCheckStatus.ALL)
        return result

    def run_check(self, client, connection=None):
        connection = connection or make_connection()
        patcher, calls = stub_api_client(client)
        with patcher:
            result = self.check(connection)
        return result, calls

    def test_a_parsed_measure_list_is_ok(self):
        result, _calls = self.run_check(FakeAPIClient(measures={"105": {"name": "Air temperature"}}))
        self.assertEqual(result.status, SourceCheckStatus.OK)
        self.assertIsNone(result.category)
        self.assertIn("102.218.136.213", result.message)
        self.assertIn("1", result.message)

    def test_bypasses_the_cache_and_bounds_the_call(self):
        _result, calls = self.run_check(FakeAPIClient(measures={}))
        self.assertEqual(calls, [{"use_cache": False, "timeout": 5, "retries": 0}])

    def test_classifies_from_the_status_the_server_sent(self):
        for status, category in ((401, "AUTH_FAILED"), (403, "PERMISSION_DENIED"),
                                 (404, "PATH_NOT_FOUND"), (500, "PROTOCOL_ERROR"),
                                 (503, "PROTOCOL_ERROR")):
            with self.subTest(status=status):
                error = requests.HTTPError(response=FakeResponse(status))
                result, _calls = self.run_check(FakeAPIClient(error=error))
                self.assertEqual(result.status, SourceCheckStatus.FAILED)
                self.assertEqual(result.category, category)
                self.assertIn(str(status), result.message)
                self.assertIn("/api/polaris/base_measures", result.message)

    def test_never_names_the_token_bearing_query_string(self):
        error = requests.HTTPError(response=FakeResponse(401))
        result, _calls = self.run_check(FakeAPIClient(error=error))
        self.assertNotIn("api_token", result.message)
        self.assertNotIn("?", result.message)

    def test_declines_a_status_that_is_not_the_sources_fault(self):
        for status in (400, 422, 429):
            with self.subTest(status=status):
                error = requests.HTTPError(response=FakeResponse(status))
                result, _calls = self.run_check(FakeAPIClient(error=error))
                self.assertEqual(result.status, SourceCheckStatus.FAILED)
                self.assertIsNone(result.category)

    def test_a_login_page_200_is_not_ok(self):
        for error in (ValueError("The response carried no 'items' list."),
                      requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0)):
            with self.subTest(error=type(error).__name__):
                result, _calls = self.run_check(FakeAPIClient(error=error))
                self.assertEqual(result.status, SourceCheckStatus.FAILED)
                self.assertIsNone(result.category)
                self.assertIn("not a base-measure list", result.message)

    def test_a_codeless_failure_declines_the_category(self):
        # Core stamps every return layer 5, so a layer-4 category here would
        # have the diagnostic contradict itself about which layer failed.
        for error in (requests.ConnectionError("connection refused"),
                      requests.exceptions.SSLError("bad handshake"),
                      requests.exceptions.ReadTimeout("timed out")):
            with self.subTest(error=type(error).__name__):
                result, _calls = self.run_check(FakeAPIClient(error=error))
                self.assertEqual(result.status, SourceCheckStatus.FAILED)
                self.assertIsNone(result.category)
                self.assertIn("could not be reached", result.message)

    def test_survives_the_core_normaliser(self):
        from adl.core.source_checks import normalise_source_check_result
        result, _calls = self.run_check(FakeAPIClient(measures={"105": {}}))
        self.assertEqual(normalise_source_check_result(result).status, SourceCheckStatus.OK)

    def test_core_detects_the_override(self):
        from adl.core.source_checks import connection_implements_check_source
        self.assertTrue(connection_implements_check_source(make_connection()))


class CheckStationSourceTests(SimpleTestCase):

    def check(self, link):
        result = link.check_station_source()
        self.assertIsInstance(result, SourceCheckResult)
        self.assertIn(result.status, SourceCheckStatus.ALL)
        return result

    def run_check(self, client, link=None):
        link = link or make_station_link()
        patcher, calls = stub_api_client(client)
        with patcher:
            result = self.check(link)
        return result, calls

    def test_a_present_id_is_ok_with_the_upstream_label(self):
        client = FakeAPIClient(stations={"42": {"id": 42, "name": "Dagoretti Corner"}})
        result, _calls = self.run_check(client)
        self.assertEqual(result.status, SourceCheckStatus.OK)
        self.assertIn("42", result.message)
        self.assertIn("Dagoretti Corner", result.message)

    def test_a_present_id_without_a_label_still_reads_cleanly(self):
        client = FakeAPIClient(stations={"42": {"id": 42}})
        result, _calls = self.run_check(client)
        self.assertEqual(result.status, SourceCheckStatus.OK)
        self.assertIn("42", result.message)

    def test_an_absent_id_is_proven_not_found(self):
        client = FakeAPIClient(stations={"99": {"id": 99, "name": "Somewhere else"}})
        result, _calls = self.run_check(client)
        self.assertEqual(result.status, SourceCheckStatus.FAILED)
        self.assertEqual(result.category, "PATH_NOT_FOUND")
        self.assertIn("42", result.message)

    def test_carries_no_invented_count(self):
        # A membership test carries identity and outcome and no number; a count
        # of our own configured mappings would look like evidence and be none.
        client = FakeAPIClient(stations={"42": {"id": 42, "name": "Dagoretti Corner"}})
        link = make_station_link(mappings=[mapping(105), mapping(106)])
        result, _calls = self.run_check(client, link=link)
        self.assertEqual(result.message,
                         'Station 42 found upstream as "Dagoretti Corner".')

    def test_bypasses_the_cache(self):
        # Harder here than at connection scope: a day-old list would report a
        # station added upstream yesterday as proven missing.
        _result, calls = self.run_check(FakeAPIClient(stations={"42": {"id": 42}}))
        self.assertEqual(calls, [{"use_cache": False, "timeout": 5, "retries": 0}])

    def test_a_failed_read_is_never_converted_into_ok(self):
        for error in (requests.ConnectionError("connection refused"),
                      requests.HTTPError(response=FakeResponse(500)),
                      ValueError("The response carried no 'items' list.")):
            with self.subTest(error=type(error).__name__):
                result, _calls = self.run_check(FakeAPIClient(error=error))
                self.assertEqual(result.status, SourceCheckStatus.FAILED)
                self.assertNotEqual(result.category, "PATH_NOT_FOUND")

    def test_core_detects_the_override(self):
        from adl.core.source_checks import station_link_implements_check_station_source
        self.assertTrue(station_link_implements_check_station_source(make_station_link()))


class SourcesCountTests(SimpleTestCase):
    """The count is committed only from something the source told us, and
    only once it has told us."""

    def make_link(self, mappings=(mapping(105),)):
        link = make_station_link(mappings=mappings)
        # The plugin logs the link, and its __str__ reaches for the station.
        link.station = Station(name="Station 1")
        link.station.network = Network(name="Polaris Network")
        return link

    def collect(self, link, client):
        patcher, _calls = stub_api_client(client)
        with patcher:
            return PolarisWebPlugin().get_station_data(link, START, END)

    def test_counts_what_the_client_reported(self):
        link = self.make_link()
        records = self.collect(link, FakeAPIClient(measurements=([{"observation_time": START}], 6)))
        self.assertEqual(link.adl_sources_count, 6)
        self.assertEqual(len(records), 1)

    def test_an_empty_answer_is_zero_not_silence(self):
        link = self.make_link()
        self.collect(link, FakeAPIClient(measurements=([], 0)))
        self.assertEqual(link.adl_sources_count, 0)

    def test_a_failed_call_makes_no_claim_at_all(self):
        # None, never 0: a run that never got an answer must not accuse the
        # source of having offered nothing.
        link = self.make_link()
        link.adl_sources_count = None
        with self.assertRaises(requests.ConnectionError):
            self.collect(link, FakeAPIClient(error=requests.ConnectionError("refused")))
        self.assertIsNone(link.adl_sources_count)

    def test_no_configured_mappings_makes_no_claim_either(self):
        # The early return happens before any network call, so there is
        # nothing the source told us and nothing to report.
        link = self.make_link(mappings=())
        link.adl_sources_count = None
        self.collect(link, FakeAPIClient(measurements=([], 0)))
        self.assertIsNone(link.adl_sources_count)

    def test_the_count_is_of_entries_not_of_our_measure_ids(self):
        # Three configured measures, one series carrying two entries: the
        # honest answer is 2. Counting measure ids would report 3 without
        # having asked the source anything.
        payload = {"series": [{"measure_id": 105, "data": {
            "2026-08-01 00:00": "21.5",
            "2026-08-01 01:00": "21.9",
        }}]}
        client = PolarisWebAPIClient(api_token="token", host=HOST)
        with mock.patch.object(client.session, "get", return_value=FakeResponse(200, payload)):
            records, count = client.get_measurements(42, [105, 106, 107], START, END)
        self.assertEqual(count, 2)
        self.assertEqual(len(records), 2)

    def test_entries_are_counted_as_carried(self):
        # A null value and an unparseable timestamp are both dropped by the
        # client; neither drop is the source's doing, so both still count.
        payload = {"series": [{"measure_id": 105, "data": {
            "2026-08-01 00:00": "21.5",
            "2026-08-01 01:00": None,
            "not-a-timestamp": "22.0",
        }}]}
        client = PolarisWebAPIClient(api_token="token", host=HOST)
        with mock.patch.object(client.session, "get", return_value=FakeResponse(200, payload)):
            records, count = client.get_measurements(42, [105], START, END)
        self.assertEqual(count, 3)
        self.assertEqual(len(records), 1)


class ExceptionStampingTests(SimpleTestCase):
    """A failed ingestion run carries the source's own verdict into the
    activity log, stamped in place so core's type table still applies."""

    def get_stations(self, response):
        client = PolarisWebAPIClient(api_token="token", host=HOST)
        with mock.patch.object(client.session, "get", return_value=response):
            return client.get_stations()

    def test_stamps_a_classified_status_at_layer_5(self):
        for status, category in ((401, "AUTH_FAILED"), (403, "PERMISSION_DENIED"),
                                 (404, "PATH_NOT_FOUND"), (502, "PROTOCOL_ERROR")):
            with self.subTest(status=status):
                with self.assertRaises(requests.HTTPError) as caught:
                    self.get_stations(FakeResponse(status))
                self.assertEqual(caught.exception.adl_category, category)
                self.assertEqual(caught.exception.adl_layer, 5)

    def test_leaves_a_declined_status_unstamped(self):
        # Declining keeps core's read-time tier free to classify the row
        # later; a stamp — UNKNOWN above all — would block it permanently.
        for status in (400, 422, 429):
            with self.subTest(status=status):
                with self.assertRaises(requests.HTTPError) as caught:
                    self.get_stations(FakeResponse(status))
                self.assertFalse(hasattr(caught.exception, "adl_category"))

    def test_core_reads_the_stamp(self):
        from adl.core.classification import classify_failure
        with self.assertRaises(requests.HTTPError) as caught:
            self.get_stations(FakeResponse(401))
        self.assertEqual(classify_failure(caught.exception), ("AUTH_FAILED", 5))

    def test_a_body_that_is_not_a_station_list_raises(self):
        for payload in (NOT_JSON, {"error": "unauthorized"}, []):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    self.get_stations(FakeResponse(200, payload))

    def test_the_status_table_declines_what_is_not_the_sources_fault(self):
        self.assertIsNone(category_for_status(302))
        self.assertIsNone(category_for_status(429))
        self.assertEqual(category_for_status(404), "PATH_NOT_FOUND")


class OlderCoreImportSafetyTests(SimpleTestCase):
    """The plugin must import cleanly on a core release that predates the
    source-check contracts, so nothing may import ``adl.core.source_checks``
    at module level.

    The contracts import it lazily instead, inside the method that needs it.
    Never wrap that import in ``try/except ImportError``: on an older core the
    method is never called, so the handler is unreachable, and it would turn a
    genuine import failure into a silent "this plugin does not support the
    check".
    """

    # Every module this plugin ships. Extend it as the plugin grows more.
    MODULES = ["models.py", "plugins.py", "client.py", "apps.py", "views.py",
               "utils.py", "forms.py", "validators.py", "widgets.py",
               "wagtail_hooks.py"]

    DENIED = "adl.core.source_checks"

    def test_no_module_level_import_of_source_checks(self):
        package_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in self.MODULES:
            path = os.path.join(package_dir, name)
            if not os.path.exists(path):
                continue  # a module this plugin does not (yet) ship
            with open(path) as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Import, ast.ImportFrom)):
                    continue
                if node.col_offset != 0:
                    continue  # indented imports are lazy, inside a function
                names = [a.name for a in node.names]
                module = getattr(node, "module", "") or ""
                self.assertNotIn(
                    self.DENIED, [module] + names,
                    f"{name} imports {self.DENIED} at module level")

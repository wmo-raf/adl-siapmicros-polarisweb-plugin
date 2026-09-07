"""A stand-in Polaris Web API, for the documentation screenshot harness.

Why this exists rather than a live account: see the "Mocking the source"
section of the capture harness README in the adl repo. A guide's screenshots
have to be reproducible from the repos alone, must expose no country's data,
and must show a *healthy* instance, which means the newest observation has to
be "now" every time the capture runs.

What is recorded and what is not
--------------------------------
The base-measure catalogue below is vendor content: the names, short names and
units are transcribed verbatim from a real Polaris Web deployment, because
they are what the guide teaches an operator to read in the *Polaris Measure*
select. They are identical for every customer.

Measure and station **ids** are demo values. The real ones identify a national
network's hardware, and they appear in no screen this guide captures — the
selects show labels, and the id only ever travels inside a request key — so
substituting them costs the documentation nothing.

Rain_Tot is served on purpose: the plugin hides it from the measure select
(it is an accumulated total, easily confused with the interval measure
Rain_Prev), and the guide documents that it does, so the catalogue has to
contain it for the screenshot to prove it.

Readings are synthesised at request time, so the freshness layer of the
ingestion diagnostic is honest rather than staged.

Clocks: a Polaris Web server reports in the local time of the stations it
serves, and ADL both writes the request window and reads the response back in
the connection's *Stations Timezone*. So this stub works entirely in SAMPLE_TZ,
which compose.mock.yml sets to the same zone as fixture.json. Emitting UTC
instead puts the newest reading three hours in the past, and the diagnostic
reports stale data on a source that is perfectly healthy.
"""
import json
import math
import os
import random
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

API_TOKEN = os.environ.get("MOCK_POLARIS_TOKEN", "demo-polaris-token")
SAMPLE_TZ = ZoneInfo(os.environ.get("SAMPLE_TZ", "Africa/Nairobi"))
INTERVAL_MINUTES = 15

STATIONS = [
    {"id": 101, "name": "Kabete Demo AWS"},
    {"id": 102, "name": "Lodwar Demo AWS"},
    {"id": 103, "name": "Mombasa Demo AWS"},
]

# name / short_name / unit verbatim from a real catalogue; ids are demo values.
MEASURES = [
    {"id": 101, "name": "Air Temperature", "short_name": "Air_T", "unit": "°C"},
    {"id": 102, "name": "Air Humidity", "short_name": "Air_RH", "unit": "%RH"},
    {"id": 103, "name": "Atmospheric Pressure", "short_name": "Atm_Press", "unit": "mBar"},
    {"id": 104, "name": "Rain Gauge Preview Period", "short_name": "Rain_Prev", "unit": "mm"},
    {"id": 105, "name": "Rain Gauge Total", "short_name": "Rain_Tot", "unit": "mm"},
    {"id": 106, "name": "Wind Speed Vector", "short_name": "WS_VECT", "unit": "m/s"},
    {"id": 107, "name": "Wind Direction Vector", "short_name": "WD_VECT", "unit": "°N"},
]

# Plausible ranges per measure id, so a captured data table reads like weather
# rather than like noise.
RANGES = {
    101: (18.0, 31.0), 102: (35.0, 92.0), 103: (1008.0, 1018.0),
    104: (0.0, 2.4), 105: (0.0, 180.0), 106: (0.2, 7.5), 107: (0.0, 359.0),
}


def _value(measure_id, when):
    """A deterministic-per-(measure, minute) reading inside the measure's range.

    Deterministic so that two runs of the capture over the same window agree,
    and so a re-shot entry does not disagree with the one beside it.
    """
    low, high = RANGES.get(int(measure_id), (0.0, 1.0))
    rnd = random.Random(f"{measure_id}-{when:%Y%m%d%H%M}")
    # a daily cycle plus a little scatter
    phase = math.sin((when.hour * 60 + when.minute) / 1440 * 2 * math.pi)
    mid = (low + high) / 2
    span = (high - low) / 2
    return round(mid + span * phase * 0.8 + rnd.uniform(-span * 0.15, span * 0.15), 2)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter compose logs
        pass

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except ValueError:
            return {}

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path.rstrip("/")

        # The token rides in the query string. Checking it is the point of a
        # stub over a static file: the guide's feedback catalogue has a row for
        # HTTP 401, and it has to be capturable on purpose.
        if (query.get("api_token") or [None])[0] != API_TOKEN:
            return self._send(401, {"detail": "Invalid API token."})

        if path == "/api/polaris/stations":
            return self._send(200, {"items": STATIONS, "total": len(STATIONS)})
        if path == "/api/polaris/base_measures":
            return self._send(200, {"items": MEASURES, "total": len(MEASURES)})
        if path == "/api/polaris/data/series":
            return self._series(self._body())
        return self._send(404, {"detail": "Not found."})

    # The client issues the series read as a GET carrying a JSON body; POST is
    # accepted too so the stub does not constrain how the plugin evolves.
    do_POST = do_GET

    def _series(self, body):
        fmt = "%Y%m%d%H%M"
        try:
            start = datetime.strptime(body["date_start"], fmt)
            end = datetime.strptime(body["date_end"], fmt)
        except (KeyError, TypeError, ValueError):
            return self._send(400, {"detail": "date_start and date_end are required."})

        # The window arrived in station-local time, so "now" is read there too.
        now = datetime.now(SAMPLE_TZ).replace(tzinfo=None)
        end = min(end, now)

        # Align to the interval, the way a logger reporting every 15 minutes does.
        step = timedelta(minutes=INTERVAL_MINUTES)
        first = start.replace(second=0, microsecond=0)
        first += timedelta(minutes=(-first.minute) % INTERVAL_MINUTES)

        series = []
        for key in (body.get("measures") or {}):
            # keys are "<station id>_<measure id>"
            _, _, measure_id = key.partition("_")
            data, when = {}, first
            while when <= end:
                data[when.strftime("%Y-%m-%d %H:%M")] = _value(measure_id, when)
                when += step
            series.append({"measure_id": int(measure_id), "data": data})

        return self._send(200, {"series": series})


if __name__ == "__main__":
    ThreadingHTTPServer(("", 80), Handler).serve_forever()

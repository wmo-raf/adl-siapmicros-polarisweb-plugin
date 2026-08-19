from urllib.parse import urlparse

import requests
from django.db import models
from django.urls import reverse
from django.utils.translation import gettext, gettext_lazy as _
from modelcluster.fields import ParentalKey
from wagtail.admin.panels import FieldPanel, MultiFieldPanel
from wagtail.models import Orderable

from adl.core.models import DataParameter, NetworkConnection, StationLink, Unit

from .client import (
    API_ROOT_PATH,
    BASE_MEASURES_PATH,
    DEFAULT_TIMEOUT,
    PolarisWebAPIClient,
    category_for_status,
)
from .validators import validate_start_date
from .widgets import PolarisStationSelectWidget, PolarisMeasureSelectWidget

# What the diagnostic's on-demand checks pass instead of the ingestion
# defaults. Core bounds its whole probe — DNS, TCP and the source check
# together — by a 15-second wall clock and abandons rather than kills a
# worker that overruns it, so the check has to come back first with a real
# verdict. Deliberately not a model field: an operator who raised it to 300
# for a slow partner would silently re-break the probe.
SOURCE_CHECK_TIMEOUT_SECONDS = 5


class PolarisWebConnection(NetworkConnection):
    """
    Stores connection credentials for a SIAP+Micros Polaris Web API instance.
    Variable mappings are defined here since base measures are shared across
    all stations.
    """
    station_link_model_string_label = "adl_siapmicros_polarisweb_plugin.PolarisWebStationLink"

    host = models.URLField(
        verbose_name=_("Host"),
        help_text=_("e.g. http://102.218.136.213:88"),
    )
    api_token = models.CharField(max_length=512, verbose_name=_("API Token"))

    panels = NetworkConnection.panels + [
        MultiFieldPanel([
            FieldPanel("host"),
            FieldPanel("api_token"),
        ], heading=_("Polaris Web API Credentials")),
    ]

    class Meta:
        verbose_name = "Polaris Web API Connection"
        verbose_name_plural = "Polaris Web API Connections"

    def get_extra_model_admin_links(self):
        return [
            {
                "label": _("Manage Variable Mappings"),
                "url": reverse("polaris_edit_variable_mappings", args=[self.id]),
                "icon_name": "list-ul",
                "kwargs": {"attrs": {"target": "_blank"}},
            }
        ]

    @property
    def source_host(self):
        """The data host this connection dials, for operator-facing messages."""
        return urlparse(self.host).hostname

    def get_api_client(self, use_cache=True, timeout=DEFAULT_TIMEOUT, retries=None):
        """
        Returns the Polaris Web API client instance.

        The defaults are the ingestion path's behaviour, unchanged. The
        diagnostic's on-demand checks pass a bounded, cache-bypassed client
        instead.
        """
        return PolarisWebAPIClient(
            api_token=self.api_token,
            host=self.host,
            timeout=timeout,
            retries=retries,
            use_cache=use_cache,
        )

    def get_source_endpoint(self):
        """
        The (host, port) core's generic DNS -> TCP probe dials (layer 4 of the
        ingestion diagnostic).

        The configured host is routinely an IP literal with a non-default port
        — the field's own help text says so — so the explicit port is what is
        reported whenever the URL carries one. Naming 443 instead would name a
        port the client never dials, and a wrong endpoint produces blocking
        layer-4 evidence that short-circuits the layers below it.
        """
        parsed = urlparse(self.host)
        return parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)

    def check_source(self):
        """
        Ask whether the source accepts our credentials and offers data
        (layer 5 of the ingestion diagnostic). Read-only, on demand only.

        The base-measure list is a shallow read with no station and no time
        argument, and it is asked for with the cache bypassed: it is otherwise
        held for 24 hours, and a cached copy would report OK while the source
        is down — the precise failure this check exists to catch.
        """
        # Imported lazily: this module does not exist on a core release
        # predating the source-check contracts, where this method is never
        # called and a module-level import would kill the whole plugin.
        from adl.core.source_checks import SourceCheckResult, SourceCheckStatus

        host = self.source_host
        # The API token rides in the query string, which is why only the path
        # is ever named — here or in a log line of our own.
        path = f"{API_ROOT_PATH}{BASE_MEASURES_PATH}"

        try:
            # Client construction belongs inside the guarded region, so a
            # credential fault reads as a check failure rather than an
            # unhandled error.
            client = self.get_api_client(use_cache=False, timeout=SOURCE_CHECK_TIMEOUT_SECONDS, retries=0)
            measures = client.get_base_measures()
        except requests.HTTPError as e:
            return SourceCheckResult(
                status=SourceCheckStatus.FAILED,
                category=category_for_status(e.response.status_code),
                message=gettext("%(host)s returned HTTP %(code)s for %(path)s.") % {
                    "host": host,
                    "code": e.response.status_code,
                    "path": path,
                },
            )
        except ValueError:
            # Ordered ahead of RequestException on purpose: requests' own
            # JSONDecodeError is both, and it belongs here. The source sent no
            # code to classify from, so the category is declined.
            return SourceCheckResult(
                status=SourceCheckStatus.FAILED,
                message=gettext("%(host)s answered, but the response was not a "
                                "base-measure list.") % {"host": host},
            )
        except requests.RequestException as e:
            return SourceCheckResult(
                status=SourceCheckStatus.FAILED,
                message=gettext("%(host)s could not be reached: %(error)s") % {
                    "host": host,
                    "error": e,
                },
            )

        return SourceCheckResult(
            status=SourceCheckStatus.OK,
            message=gettext("%(host)s accepted our credentials and returned "
                            "%(count)s base measure(s).") % {
                "host": host,
                "count": len(measures),
            },
        )


class PolarisWebVariableMapping(Orderable):
    """
    Maps a Polaris base_measure to an ADL DataParameter.
    Defined at the connection level since measures are the same for all stations.
    """
    network_connection = ParentalKey(
        PolarisWebConnection,
        on_delete=models.CASCADE,
        related_name="variable_mappings",
    )
    adl_parameter = models.ForeignKey(
        DataParameter,
        on_delete=models.CASCADE,
        verbose_name=_("ADL Parameter"),
    )
    polaris_measure_id = models.IntegerField(verbose_name=_("Polaris Measure"))
    polaris_measure_unit = models.ForeignKey(
        Unit,
        on_delete=models.CASCADE,
        verbose_name=_("Polaris Measure Unit"),
    )

    panels = [
        FieldPanel("adl_parameter"),
        FieldPanel("polaris_measure_id", widget=PolarisMeasureSelectWidget),
        FieldPanel("polaris_measure_unit"),
    ]

    @property
    def source_parameter_name(self):
        """Key used in parsed records dict — stringified measure ID."""
        return str(self.polaris_measure_id)

    @property
    def source_parameter_unit(self):
        return self.polaris_measure_unit


class PolarisWebStationLink(StationLink):
    """
    Links an ADL station to a Polaris Web station ID.
    """
    polaris_station_id = models.IntegerField(verbose_name=_("Polaris Station"))

    start_date = models.DateTimeField(
        blank=True,
        null=True,
        validators=[validate_start_date],
        verbose_name=_("Collection Start Date"),
        help_text=_(
            "Collection never starts before this date. On the first run it is "
            "the start of the backfill; afterwards, moving it forward past the "
            "latest saved record skips the gap. Leave empty to start from the "
            "last 24 hours."
        ),
    )

    panels = StationLink.panels + [
        FieldPanel("polaris_station_id", widget=PolarisStationSelectWidget),
        FieldPanel("start_date"),
    ]

    class Meta:
        verbose_name = "Polaris Web Station Link"
        verbose_name_plural = "Polaris Web Station Links"

    def __str__(self):
        return f"Polaris Station {self.polaris_station_id} → {self.station}"

    def get_variable_mappings(self):
        """Variable mappings are defined on the connection, shared across all
        stations."""
        return self.network_connection.variable_mappings.all()

    def get_first_collection_date(self):
        return self.start_date

    def check_station_source(self):
        """
        Ask whether this station's Polaris id resolves at the source (layer 5
        of the ingestion diagnostic, station-scoped).

        Polaris offers no per-station lookup, but the station list already
        comes back keyed by id, so the check is a membership test over it. The
        cache is bypassed over the whole check rather than only its failure
        branch: a day-old list would report a station added upstream yesterday
        as missing, causing the very misconfiguration the check exists to
        detect.
        """
        from adl.core.source_checks import SourceCheckResult, SourceCheckStatus

        connection = self.network_connection
        host = connection.source_host

        try:
            client = connection.get_api_client(use_cache=False, timeout=SOURCE_CHECK_TIMEOUT_SECONDS, retries=0)
            stations = client.get_stations()
        except ValueError:
            return SourceCheckResult(
                status=SourceCheckStatus.FAILED,
                message=gettext("%(host)s answered, but the response was not a "
                                "station list.") % {"host": host},
            )
        except requests.RequestException as e:
            return SourceCheckResult(
                status=SourceCheckStatus.FAILED,
                message=gettext("Could not read the station list from %(host)s: "
                                "%(error)s") % {"host": host, "error": e},
            )

        station = stations.get(str(self.polaris_station_id))

        if station is None:
            # Absent from a list the source really returned is proof, not
            # suspicion: this station link can never ingest anything.
            return SourceCheckResult(
                status=SourceCheckStatus.FAILED,
                category="PATH_NOT_FOUND",
                message=gettext("Station %(id)s was not found in the source's "
                                "station list.") % {"id": self.polaris_station_id},
            )

        # The upstream's own label is what catches a valid-but-wrong id — a
        # real station belonging to a different site — which is the failure
        # that yields plausible wrong data rather than an outage.
        label = station.get("name") or ""
        context = {"id": self.polaris_station_id, "label": label}

        if label:
            message = gettext('Station %(id)s found upstream as "%(label)s".') % context
        else:
            message = gettext("Station %(id)s was found in the source's station "
                              "list.") % context

        return SourceCheckResult(status=SourceCheckStatus.OK, message=message)

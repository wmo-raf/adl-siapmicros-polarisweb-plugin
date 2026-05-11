from django.db import models
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from modelcluster.fields import ParentalKey
from wagtail.admin.panels import FieldPanel, InlinePanel, MultiFieldPanel
from wagtail.models import Orderable

from adl.core.models import DataParameter, NetworkConnection, StationLink, Unit

from .client import PolarisWebAPIClient
from .validators import validate_start_date
from .widgets import PolarisStationSelectWidget, PolarisMeasureSelectWidget


class PolarisWebConnection(NetworkConnection):
    """
    Stores connection credentials for a SIAP+Micros Polaris Web API instance.
    Variable mappings are defined here since base measures are shared across all stations.
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
    
    def get_api_client(self):
        return PolarisWebAPIClient(
            api_token=self.api_token,
            host=self.host,
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
        verbose_name=_("Initial Collection Start Date"),
        help_text=_(
            "The date to start collecting data on the first run. "
            "Ignored if data has already been collected for this station."
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
        """Variable mappings are defined on the connection, shared across all stations."""
        return self.network_connection.variable_mappings.all()
    
    def get_first_collection_date(self):
        return self.start_date

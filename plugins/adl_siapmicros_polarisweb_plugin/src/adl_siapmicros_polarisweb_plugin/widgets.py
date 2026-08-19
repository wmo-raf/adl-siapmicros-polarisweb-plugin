from django.forms import Widget
from django.urls import reverse


class PolarisStationSelectWidget(Widget):
    template_name = "adl_siapmicros_polarisweb_plugin/widgets/polaris_station_select_widget.html"

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        context.update({
            "polaris_stations_url": reverse("polaris_stations_for_connection"),
        })
        return context


class PolarisMeasureSelectWidget(Widget):
    template_name = "adl_siapmicros_polarisweb_plugin/widgets/polaris_measure_select_widget.html"

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        context.update({
            "polaris_measures_url": reverse("polaris_measures_for_connection"),
        })
        return context

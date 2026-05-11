from django.urls import path
from wagtail import hooks

from .views import (
    edit_variable_mappings,
    get_polaris_measures_for_connection,
    get_polaris_stations_for_connection,
)


@hooks.register("register_admin_urls")
def urlconf_polarisweb_plugin():
    return [
        path(
            "adl-polarisweb-plugin/polaris-conn-stations/",
            get_polaris_stations_for_connection,
            name="polaris_stations_for_connection",
        ),
        path(
            "adl-polarisweb-plugin/polaris-conn-measures/",
            get_polaris_measures_for_connection,
            name="polaris_measures_for_connection",
        ),
        path(
            "adl-polarisweb-plugin/variable-mappings/<int:connection_id>/",
            edit_variable_mappings,
            name="polaris_edit_variable_mappings",
        ),
    ]

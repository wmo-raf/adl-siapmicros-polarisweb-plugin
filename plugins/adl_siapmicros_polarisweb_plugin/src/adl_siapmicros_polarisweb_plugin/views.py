import logging

from adl.core.utils import get_object_or_none
from django.forms import inlineformset_factory
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _

from .forms import PolarisWebVariableMappingForm
from .models import PolarisWebConnection, PolarisWebVariableMapping
from .utils import get_measures, get_stations

logger = logging.getLogger(__name__)

VariableMappingFormSet = inlineformset_factory(
    PolarisWebConnection,
    PolarisWebVariableMapping,
    form=PolarisWebVariableMappingForm,
    extra=1,
    can_delete=True,
)


def get_polaris_stations_for_connection(request):
    network_connection_id = request.GET.get("connection_id")
    
    if not network_connection_id:
        return JsonResponse({"error": _("Network connection ID is required.")}, status=400)
    
    network_conn = get_object_or_none(PolarisWebConnection, pk=network_connection_id)
    if not network_conn:
        return JsonResponse(
            {"error": _("The selected connection is not a Polaris Web API Connection.")},
            status=400,
        )
    
    stations_list = get_stations(network_conn)
    return JsonResponse(stations_list, safe=False)


def get_polaris_measures_for_connection(request):
    network_connection_id = request.GET.get("connection_id")
    
    if not network_connection_id:
        return JsonResponse({"error": _("Network connection ID is required.")}, status=400)
    
    network_conn = get_object_or_none(PolarisWebConnection, pk=network_connection_id)
    if not network_conn:
        return JsonResponse(
            {"error": _("The selected connection is not a Polaris Web API Connection.")},
            status=400,
        )
    
    measures_list = get_measures(network_conn)
    
    if not measures_list:
        return JsonResponse(
            {"error": _("No measures found for the selected connection.")},
            status=404,
        )
    
    return JsonResponse(measures_list, safe=False)


def edit_variable_mappings(request, connection_id):
    connection = get_object_or_404(PolarisWebConnection, pk=connection_id)
    
    # Load measures from API to populate the select widget choices
    try:
        measures_list = get_measures(connection)
        measure_choices = [("", "---------")] + [
            (m["value"], m["label"]) for m in measures_list
        ]
    except Exception as e:
        logger.error("Failed to fetch measures from Polaris API: %s", e)
        measures_list = []
        measure_choices = [("", "---------")]
    
    if request.method == "POST":
        formset = VariableMappingFormSet(
            request.POST,
            instance=connection,
            form_kwargs={"measure_choices": measure_choices},
        )
        if formset.is_valid():
            formset.save()
            return redirect(request.path)
    else:
        formset = VariableMappingFormSet(
            instance=connection,
            form_kwargs={"measure_choices": measure_choices},
        )
    
    return render(
        request,
        "adl_siapmicros_polarisweb_plugin/variable_mappings.html",
        {
            "connection": connection,
            "formset": formset,
        },
    )

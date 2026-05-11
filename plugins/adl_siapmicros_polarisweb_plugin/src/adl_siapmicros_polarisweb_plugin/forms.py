from django import forms
from django.utils.translation import gettext_lazy as _

from adl.core.models import DataParameter, Unit

from .models import PolarisWebVariableMapping


class PolarisWebVariableMappingForm(forms.ModelForm):
    class Meta:
        model = PolarisWebVariableMapping
        fields = ["adl_parameter", "polaris_measure_id", "polaris_measure_unit"]

    def __init__(self, *args, measure_choices=None, **kwargs):
        super().__init__(*args, **kwargs)

        if measure_choices:
            self.fields["polaris_measure_id"] = forms.ChoiceField(
                choices=measure_choices,
                label=_("Polaris Measure"),
            )

        self.fields["adl_parameter"].queryset = DataParameter.objects.all().order_by("name")
        self.fields["polaris_measure_unit"].queryset = Unit.objects.all().order_by("name")

    def clean_polaris_measure_id(self):
        value = self.cleaned_data.get("polaris_measure_id")
        try:
            return int(value)
        except (TypeError, ValueError):
            raise forms.ValidationError(_("Please select a valid measure."))

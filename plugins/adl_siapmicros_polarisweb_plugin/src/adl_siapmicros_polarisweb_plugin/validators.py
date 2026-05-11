import datetime

from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


def validate_start_date(value: datetime.datetime):
    """
    Ensure the collection start date is in the past.
    """
    if value is not None and value > timezone.now():
        raise ValidationError(_("Start date must be in the past."))

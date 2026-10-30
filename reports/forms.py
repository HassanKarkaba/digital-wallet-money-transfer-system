from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django import forms

from payments.forms import StrictForm


class StatementForm(StrictForm):
    start = forms.DateField(label='Start date · UTC', widget=forms.DateInput(attrs={'type': 'date'}))
    end = forms.DateField(
        label='End date · UTC (exclusive)',
        widget=forms.DateInput(attrs={'type': 'date'}),
        help_text='The end date is excluded. Select at most 366 days.',
    )
    timezone = forms.CharField(
        initial='UTC',
        max_length=64,
        label='Display timezone',
        help_text='IANA timezone; UTC timestamps remain canonical in the statement.',
    )

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('start') and cleaned.get('end'):
            days = (cleaned['end'] - cleaned['start']).days
            if days < 1 or days > 366:
                raise forms.ValidationError('Select a positive period no longer than 366 days.')
            cleaned['start_at'] = datetime.combine(cleaned['start'], time.min, tzinfo=timezone.utc)
            cleaned['end_at'] = datetime.combine(cleaned['end'], time.min, tzinfo=timezone.utc)
        try:
            if cleaned.get('timezone'):
                ZoneInfo(cleaned['timezone'])
        except (ValueError, ZoneInfoNotFoundError):
            self.add_error('timezone', 'Enter a valid IANA timezone.')
        return cleaned

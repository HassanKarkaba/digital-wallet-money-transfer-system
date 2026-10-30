import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django import forms


class StrictForm(forms.Form):
    """Reject unknown business fields instead of silently dropping instruction input."""

    extra_allowed = {'csrfmiddlewaretoken', 'submit'}

    def clean(self):
        cleaned = super().clean()
        if self.is_bound:
            unknown = set(self.data) - set(self.fields) - self.extra_allowed
            if unknown:
                raise forms.ValidationError('This form contains unsupported fields. Start with a fresh form.')
            if hasattr(self.data, 'getlist') and any(
                len(self.data.getlist(key)) != 1 for key in self.data if key not in self.extra_allowed
            ):
                raise forms.ValidationError('Each instruction field must be submitted once.')
        return cleaned


class MoneyField(forms.CharField):
    def __init__(self, **kwargs):
        kwargs.setdefault('max_length', 16)
        kwargs.setdefault('help_text', 'USD · $0.01 to $2,500.00 · no fees')
        kwargs.setdefault(
            'widget',
            forms.TextInput(attrs={'inputmode': 'decimal', 'placeholder': '0.00', 'autocomplete': 'off'}),
        )
        super().__init__(**kwargs)

    def clean(self, value):
        value = super().clean(value).strip()
        if not re.fullmatch(r'[0-9]+(?:\.[0-9]{1,2})?', value, flags=re.ASCII):
            raise forms.ValidationError('Enter a positive USD amount with up to two decimal places.')
        whole, _, fraction = value.partition('.')
        cents = int(whole) * 100 + int((fraction + '00')[:2])
        if not 1 <= cents <= 250000:
            raise forms.ValidationError('Enter an amount from $0.01 to $2,500.00.')
        return value


class PaymentForm(StrictForm):
    recipient_handle = forms.RegexField(
        r'^[a-z0-9_]{3,30}$',
        max_length=30,
        label='Recipient handle',
        help_text='Use an exact handle. We never show a public customer directory.',
    )
    amount = MoneyField()
    note = forms.CharField(
        required=False,
        max_length=280,
        widget=forms.Textarea(attrs={'rows': 3}),
        help_text='Optional · visible to the recipient',
    )
    policy_version = forms.CharField(initial='DWMT-P1', widget=forms.HiddenInput)


class RailForm(StrictForm):
    destination_id = forms.ChoiceField(label='Synthetic destination')
    amount = MoneyField()
    note = forms.CharField(required=False, max_length=280, widget=forms.Textarea(attrs={'rows': 2}))
    policy_version = forms.CharField(initial='DWMT-P1', widget=forms.HiddenInput)

    def __init__(self, *args, destinations=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['destination_id'].choices = [
            (str(d.id), d.label + ' · synthetic USD') for d in destinations
        ]


class ProofForm(StrictForm):
    intent_id = forms.UUIDField(widget=forms.HiddenInput)
    grant_id = forms.UUIDField(required=False, widget=forms.HiddenInput)
    schema_version = forms.IntegerField(initial=1, widget=forms.HiddenInput)
    otp = forms.RegexField(
        r'^[0-9]{6}$',
        label='Authenticator code',
        max_length=6,
        min_length=6,
        widget=forms.TextInput(
            attrs={'inputmode': 'numeric', 'autocomplete': 'one-time-code', 'pattern': '[0-9]{6}'}
        ),
    )
    action_key = forms.UUIDField(widget=forms.HiddenInput)
    policy_version = forms.CharField(initial='DWMT-P1', widget=forms.HiddenInput)


class RecipientForm(StrictForm):
    handle = forms.RegexField(r'^[a-z0-9_]{3,30}$', max_length=30, label='Exact recipient handle')
    label = forms.CharField(required=False, max_length=100, help_text='A private nickname for this recipient')


class RequestForm(StrictForm):
    payer_handle = forms.RegexField(r'^[a-z0-9_]{3,30}$', max_length=30, label='Payer handle')
    amount = MoneyField()
    note = forms.CharField(required=False, max_length=280, widget=forms.Textarea(attrs={'rows': 3}))


class ScheduleForm(PaymentForm):
    local_time = forms.CharField(
        label='Local date and time',
        widget=forms.DateTimeInput(attrs={'type': 'datetime-local'}),
        help_text='At least 60 seconds and no more than 30 days after confirmation.',
    )
    timezone = forms.CharField(
        initial='UTC', max_length=64, label='IANA timezone', help_text='For example America/Detroit or UTC'
    )
    utc_offset = forms.CharField(
        label='UTC offset at that instant',
        max_length=6,
        initial='+00:00',
        help_text='For example -04:00. Required to resolve daylight saving ambiguity.',
    )

    def clean(self):
        cleaned = super().clean()
        if not all(cleaned.get(x) for x in ('local_time', 'timezone', 'utc_offset')):
            return cleaned
        try:
            zone = ZoneInfo(cleaned['timezone'])
            local = datetime.fromisoformat(cleaned['local_time'])
            if local.tzinfo is not None:
                raise ValueError
            offset = cleaned['utc_offset']
            if not re.fullmatch(r'[+-][0-9]{2}:[0-9]{2}', offset):
                raise ValueError
            expected = (int(offset[1:3]) * 60 + int(offset[4:])) * (1 if offset[0] == '+' else -1)
            candidates = []
            for fold in (0, 1):
                aware = local.replace(tzinfo=zone, fold=fold)
                utc = aware.astimezone(timezone.utc)
                if (
                    utc.astimezone(zone).replace(tzinfo=None) == local
                    and int(aware.utcoffset().total_seconds() // 60) == expected
                ):
                    candidates.append(utc)
            if not candidates:
                raise forms.ValidationError(
                    'This local time does not exist in that timezone or does not match the chosen offset.'
                )
            cleaned['due_at'] = candidates[0].isoformat().replace('+00:00', 'Z')
        except (ValueError, ZoneInfoNotFoundError):
            raise forms.ValidationError('Enter a valid local date, IANA timezone, and UTC offset.')
        return cleaned


class DestinationForm(StrictForm):
    label = forms.CharField(
        max_length=80,
        initial='Demo bank · •••• 4242',
        label='Synthetic destination label',
        help_text='Use a fictional label only. Do not enter real bank information.',
    )
    policy_version = forms.CharField(initial='DWMT-P1', widget=forms.HiddenInput)


class DisputeForm(StrictForm):
    reason = forms.CharField(min_length=10, max_length=1000, widget=forms.Textarea(attrs={'rows': 3}))
    evidence = forms.CharField(
        required=False,
        max_length=4000,
        label='Plain text evidence summary',
        widget=forms.Textarea(attrs={'rows': 4}),
    )


class VersionForm(StrictForm):
    version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)

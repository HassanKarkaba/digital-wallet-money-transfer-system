import re
import unicodedata
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django import forms
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError


class StrictForm(forms.Form):
    transport_fields = {'csrfmiddlewaretoken', 'next'}

    def clean(self):
        cleaned = super().clean()
        if self.is_bound:
            unknown = set(self.data) - set(self.fields) - self.transport_fields
            if unknown:
                raise ValidationError('This form contains unsupported fields. Reload it and try again.')
            if hasattr(self.data, 'getlist') and any(
                len(self.data.getlist(key)) != 1 for key in self.data if key not in self.transport_fields
            ):
                raise ValidationError('Each field must be submitted once.')
        return cleaned


def clean_email(value):
    value = value.strip().lower()
    if not value.isascii() or len(value) > 254:
        raise ValidationError('Use an ASCII email address of at most 254 characters.')
    return value


def clean_name(value):
    value = value.strip()
    if not value or any(unicodedata.category(ch).startswith('C') for ch in value):
        raise ValidationError('Enter a name without control characters.')
    return value


class RegistrationForm(StrictForm):
    display_name = forms.CharField(max_length=100, label='Your name')
    handle = forms.CharField(
        min_length=3,
        max_length=30,
        help_text='3 to 30 letters, numbers or underscores. This public handle is permanent.',
    )
    email = forms.EmailField(max_length=254)
    password = forms.CharField(
        min_length=15,
        max_length=128,
        strip=False,
        widget=forms.PasswordInput,
        help_text='15 to 128 characters. Spaces and Unicode are welcome.',
    )

    def clean_display_name(self):
        return clean_name(self.cleaned_data['display_name'])

    def clean_handle(self):
        handle = self.cleaned_data['handle'].lower()
        if not re.fullmatch(r'[a-z0-9_]{3,30}', handle):
            raise ValidationError('Use lowercase letters, numbers and underscores.')
        return handle

    def clean_email(self):
        return clean_email(self.cleaned_data['email'])

    def clean_password(self):
        value = self.cleaned_data['password']
        validate_password(value)
        return value


class LoginForm(StrictForm):
    email = forms.EmailField(max_length=254)
    password = forms.CharField(max_length=128, strip=False, widget=forms.PasswordInput)

    def clean_email(self):
        return clean_email(self.cleaned_data['email'])


class EmailForm(StrictForm):
    email = forms.EmailField(max_length=254)

    def clean_email(self):
        return clean_email(self.cleaned_data['email'])


class OtpForm(StrictForm):
    otp = forms.RegexField(
        r'^[0-9]{6}$',
        min_length=6,
        max_length=6,
        label='Authenticator code',
        widget=forms.TextInput(
            attrs={'inputmode': 'numeric', 'autocomplete': 'one-time-code', 'pattern': '[0-9]{6}'}
        ),
    )


class PasswordResetForm(StrictForm):
    password = forms.CharField(
        min_length=15,
        max_length=128,
        strip=False,
        widget=forms.PasswordInput,
        label='New password',
        help_text='15 to 128 characters.',
    )

    def clean_password(self):
        value = self.cleaned_data['password']
        validate_password(value)
        return value


class ProfileForm(StrictForm):
    display_name = forms.CharField(max_length=100, label='Your name')
    timezone = forms.CharField(
        max_length=64, help_text='An IANA timezone, for example America/Detroit or UTC.'
    )

    def clean_display_name(self):
        return clean_name(self.cleaned_data['display_name'])

    def clean_timezone(self):
        value = self.cleaned_data['timezone']
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValidationError('Enter a recognized IANA timezone.') from None
        return value


class EmailChangeForm(OtpForm):
    email = forms.EmailField(max_length=254, label='New email address')
    password = forms.CharField(max_length=128, strip=False, widget=forms.PasswordInput)

    def clean_email(self):
        return clean_email(self.cleaned_data['email'])


class RecoveryForm(StrictForm):
    email = forms.EmailField(max_length=254)
    password = forms.CharField(max_length=128, strip=False, widget=forms.PasswordInput)
    recovery_code = forms.CharField(max_length=64, widget=forms.PasswordInput, label='Recovery code')

    def clean_email(self):
        return clean_email(self.cleaned_data['email'])


class SessionRevokeForm(OtpForm):
    password = forms.CharField(max_length=128, strip=False, widget=forms.PasswordInput)


class StaffAccessForm(StrictForm):
    email = forms.EmailField(max_length=254, label='Exact local account email')
    role = forms.ChoiceField(
        choices=[
            ('SUPPORT', 'Support'),
            ('REVIEWER', 'Reviewer'),
            ('FINANCE', 'Finance'),
            ('ACCESS_ADMIN', 'Access administrator'),
        ]
    )
    action = forms.ChoiceField(
        choices=[
            ('INVITE', 'Invite to register for this role'),
            ('GRANT', 'Grant role'),
            ('REVOKE', 'Revoke role'),
        ]
    )

    def clean_email(self):
        value = clean_email(self.cleaned_data['email'])
        if not value.endswith('.test'):
            raise ValidationError('Staff access in this local demonstration uses synthetic .test accounts.')
        return value

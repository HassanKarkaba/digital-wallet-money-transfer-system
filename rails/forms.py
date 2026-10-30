from django import forms


class DestinationForm(forms.Form):
    label = forms.CharField(
        max_length=80,
        label='Synthetic destination label',
        help_text='Use a nickname only. Do not enter real account or routing numbers.',
    )

    def clean_label(self):
        value = self.cleaned_data['label'].strip()
        if not value or any(ord(c) < 32 for c in value):
            raise forms.ValidationError('Enter a label without control characters.')
        return value

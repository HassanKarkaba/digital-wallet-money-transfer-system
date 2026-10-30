from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from .forms import MoneyField, PaymentForm, ScheduleForm


class BrowserInputBoundaryTests(SimpleTestCase):
    def test_money_parser_rejects_ambiguous_notation(self):
        field = MoneyField()
        for text in ('0', '-1', '+1', '1e2', '1,000', '1.001', 'NaN', '١٢', '1 2', '2500.01'):
            with self.subTest(text=text), self.assertRaises(ValidationError):
                field.clean(text)
        self.assertEqual(field.clean(' 1.00 '), '1.00')

    def test_unknown_fields_are_not_silently_discarded(self):
        form = PaymentForm(
            {
                'recipient_handle': 'recipient',
                'amount': '1',
                'note': '',
                'policy_version': 'DWMT-P1',
                'sender_id': 'invented',
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn('unsupported', str(form.non_field_errors()))

    def test_nonexistent_time_is_rejected_and_ambiguous_time_requires_matching_offset(self):
        base = {
            'recipient_handle': 'recipient',
            'amount': '1',
            'note': '',
            'policy_version': 'DWMT-P1',
            'timezone': 'America/Detroit',
        }
        gap = ScheduleForm({**base, 'local_time': '2026-03-08T02:30', 'utc_offset': '-05:00'})
        self.assertFalse(gap.is_valid())
        wrong = ScheduleForm({**base, 'local_time': '2026-11-01T01:30', 'utc_offset': '-06:00'})
        self.assertFalse(wrong.is_valid())
        earlier = ScheduleForm({**base, 'local_time': '2026-11-01T01:30', 'utc_offset': '-04:00'})
        later = ScheduleForm({**base, 'local_time': '2026-11-01T01:30', 'utc_offset': '-05:00'})
        self.assertTrue(earlier.is_valid(), earlier.errors)
        self.assertTrue(later.is_valid(), later.errors)
        self.assertNotEqual(earlier.cleaned_data['due_at'], later.cleaned_data['due_at'])

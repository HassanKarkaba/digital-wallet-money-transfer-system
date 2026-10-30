from collections import Counter
from html.parser import HTMLParser

from django.template.loader import render_to_string
from django.test import SimpleTestCase

from .forms import PaymentForm


class ErrorDescriptionMarkup(HTMLParser):
    def __init__(self, field_id):
        super().__init__()
        self.field_id = field_id
        self.error_id = field_id + '_error'
        self.ids = []
        self.divs = []
        self.input_attributes = {}
        self.error_messages = []
        self.paragraph = None

    def handle_starttag(self, tag, attributes):
        values = dict(attributes)
        if values.get('id'):
            self.ids.append(values['id'])
        if tag == 'div':
            self.divs.append(values.get('id'))
        if tag == 'input' and values.get('id') == self.field_id:
            self.input_attributes = values
        if tag == 'p' and 'field-error' in values.get('class', '').split() and self.error_id in self.divs:
            self.paragraph = []

    def handle_data(self, data):
        if self.paragraph is not None:
            self.paragraph.append(data)

    def handle_endtag(self, tag):
        if tag == 'p' and self.paragraph is not None:
            self.error_messages.append(''.join(self.paragraph).strip())
            self.paragraph = None
        if tag == 'div':
            self.divs.pop()


class PaymentFormMarkupTests(SimpleTestCase):
    def test_multiple_errors_share_one_unique_aria_description_container(self):
        form = PaymentForm(
            {
                'recipient_handle': '+' * 31,
                'amount': '1.00',
                'note': '',
                'policy_version': 'DWMT-P1',
            }
        )
        self.assertFalse(form.is_valid())
        errors = [str(error) for error in form.errors['recipient_handle']]
        self.assertEqual(len(errors), 2)
        markup = ErrorDescriptionMarkup(form['recipient_handle'].id_for_label)
        markup.feed(render_to_string('payments/_form.html', {'form': form}))
        counts = Counter(markup.ids)
        self.assertEqual(len(markup.ids), len(counts), 'All rendered error and control ids must be unique')
        self.assertEqual(counts[markup.error_id], 1)
        self.assertIn(markup.error_id, markup.input_attributes.get('aria-describedby', '').split())
        self.assertEqual(markup.input_attributes.get('aria-invalid'), 'true')
        self.assertCountEqual(markup.error_messages, errors)

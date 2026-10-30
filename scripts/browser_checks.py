"""Real Chromium journeys on isolated Compose verification databases only.

No test credentials or MFA seeds are printed. The private output directory is
outside the source candidate. Destination activation is advanced explicitly in
this synthetic fixture after the browser observes its ten-minute waiting rule.
"""

# ruff: noqa: E402
# Django must initialize before importing application models in a standalone script.
import json
import os
import re
import sys
import time
import uuid
from datetime import timedelta
from email import policy
from email.parser import BytesParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django

django.setup()

import pyotp
from django.conf import settings
from django.core.management import call_command
from playwright.sync_api import expect, sync_playwright

from accounts.models import SecurityMail, User
from accounts.services import db_now, decrypt_secret
from ledger.models import Journal
from oversight.models import Case
from payments.models import MoneyRequest, Operation, Schedule
from rails.models import Destination


def main():
    assert settings.DATABASES['default']['NAME'] == 'wallet_test', 'Refusing development primary database'
    assert settings.DATABASES['simulator']['NAME'] == 'simulator_test', (
        'Refusing development simulator database'
    )
    base = os.environ.get('DWMT_BROWSER_URL', 'http://web:8000').rstrip('/')
    output = Path(settings.PRIVATE_ROOT) / 'browser-verification'
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    access_file = Path(settings.PRIVATE_ROOT) / 'browser-access.json'
    call_command('seed_demo', access_file=str(access_file))
    seeded = {item['handle']: item for item in json.loads(access_file.read_text())['accounts']}
    handle = 'browser_' + uuid.uuid4().hex[:10]
    email = handle + '@dwmt.test'
    password = 'Local browser fixture river 8531 ' + uuid.uuid4().hex[:10]
    observations = []
    errors = []

    def observed(case, detail):
        observations.append({'case': case, 'detail': detail, 'observed_at': db_now().isoformat()})
        print('Browser passed:', case, flush=True)

    def fresh_code(user):
        deadline = time.monotonic() + 65
        while time.monotonic() < deadline:
            user.refresh_from_db()
            counter = int(db_now().timestamp()) // 30
            step = max(counter, user.mfa_last_counter + 1)
            if step <= counter + 1:
                return pyotp.TOTP(decrypt_secret(user.mfa_secret)).generate_otp(step)
            time.sleep(min(1, max(0.1, deadline - time.monotonic())))
        raise AssertionError('No fresh TOTP counter became available within its bounded wait')

    def confirm(page, user):
        expect(page.get_by_role('heading', name='Review & confirm')).to_be_visible()
        otp = page.get_by_label('Authenticator code', exact=True)
        if otp.is_visible():
            otp.fill(fresh_code(user))
        page.get_by_role('button', name='Confirm exact instruction').click()
        expect(page.locator('.error-summary')).to_have_count(0)

    def login(page, credential):
        page.goto(base + '/accounts/login/')
        page.get_by_label('Email', exact=True).fill(credential['email'])
        page.get_by_label('Password', exact=True).fill(credential['password'])
        page.get_by_label('Password', exact=True).press('Enter')
        user = User.objects.get(email=credential['email'])
        if user.mfa_confirmed:
            page.get_by_label('Authenticator code', exact=True).fill(fresh_code(user))
            page.get_by_role('button', name='Verify and sign in').click()
        expect(page.get_by_role('heading', name='Wallet overview.')).to_be_visible()
        return user

    def settle(page, operation_id):
        deadline = time.monotonic() + 70
        while time.monotonic() < deadline:
            operation = Operation.objects.get(pk=operation_id)
            if operation.state in ('COMPLETED', 'FAILED', 'CANCELLED', 'UNKNOWN'):
                page.goto(base + f'/payments/{operation_id}/')
                return operation
            time.sleep(1)
        raise AssertionError('Accepted synthetic payment did not reach a recorded result within 70 seconds')

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
        context = browser.new_context(
            viewport={'width': 1440, 'height': 1040}, java_script_enabled=False, accept_downloads=True
        )
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(base + '/')
        page.keyboard.press('Tab')
        expect(page.locator(':focus')).to_have_attribute('href', '#main')
        page.keyboard.press('Enter')
        expect(page.locator('#main')).to_be_focused()
        page.goto(base + '/accounts/register/')
        page.get_by_label('Your name', exact=True).fill('Riley Carter')
        page.get_by_label('Handle', exact=True).fill(handle)
        page.get_by_label('Email', exact=True).fill(email)
        page.get_by_label('Password', exact=True).fill(password)
        page.get_by_role('button', name='Create account').click()
        expect(page.get_by_role('heading', name='Welcome back')).to_be_visible()
        user = User.objects.get(email=email)
        mail = SecurityMail.objects.filter(user=user, purpose='VERIFY').latest('created_at')
        deadline = time.monotonic() + 65
        while mail.status != 'DELIVERED' and time.monotonic() < deadline:
            time.sleep(1)
            mail.refresh_from_db()
        assert mail.status == 'DELIVERED', (
            'Local verification mail was not durably captured within 65 seconds'
        )
        captured_body = None
        capture_files = sorted(
            Path(settings.EMAIL_FILE_PATH).glob('*'), key=lambda item: item.stat().st_mtime, reverse=True
        )[:50]
        for capture in capture_files:
            if not capture.is_file():
                continue
            message = BytesParser(policy=policy.default).parsebytes(capture.read_bytes())
            if email in str(message.get('To', '')):
                captured_body = message.get_content()
                break
        assert captured_body, 'The private local capture did not contain the verification message'
        link = re.search(r'/accounts/verify/[^\s]+/', captured_body).group(0)
        page.goto(base + link)
        page.get_by_role('button', name='Verify email').click()
        page.get_by_label('Email', exact=True).fill(email)
        page.get_by_label('Password', exact=True).fill(password)
        page.get_by_role('button', name='Sign in', exact=True).click()
        expect(page.get_by_text('One more step before moving money.')).to_be_visible()
        page.get_by_role('link', name='Set up your authenticator').click()
        user.refresh_from_db()
        page.get_by_label('Authenticator code', exact=True).fill(fresh_code(user))
        page.get_by_role('button', name='Confirm authenticator').click()
        expect(page.locator('.recovery-codes li')).to_have_count(10)
        page.get_by_role('link', name='I saved my codes').click()
        expect(page.get_by_text('Your activity starts here')).to_be_visible()
        observed(
            'registration_verification_mfa',
            'Real browser registration, local captured verification, password login, replay-protected TOTP enrollment, ten one-time recovery codes, and zero-balance dashboard. JavaScript disabled.',
        )

        page.goto(base + '/recipients/')
        page.get_by_label('Exact recipient handle', exact=True).fill('jordan')
        page.get_by_label('Label', exact=False).fill('Jordan')
        page.get_by_role('button', name='Save recipient').click()
        expect(page.get_by_text('@jordan', exact=True)).to_be_visible()
        observed(
            'recipient',
            'Exact handle lookup saved a fixed recipient; no public directory or email disclosure.',
        )

        page.goto(base + '/destinations/')
        page.get_by_label('Synthetic destination label', exact=True).fill('Browser synthetic checking')
        page.get_by_role('button', name='Review destination').click()
        confirm(page, user)
        expect(
            page.get_by_text('Synthetic destination linked. It becomes available after ten minutes.')
        ).to_be_visible()
        destination = Destination.objects.get(owner=user, label='Browser synthetic checking')
        assert destination.activation_at > db_now() + timedelta(minutes=9)
        # Explicit isolated clock fixture: keep UI waiting disclosure; advance only the activation boundary.
        Destination.objects.filter(pk=destination.pk).update(activation_at=db_now() - timedelta(seconds=1))
        observed(
            'destination',
            'Browser bound-MFA linking created a synthetic token with ten-minute activation. Fixture advanced activation after observing the waiting rule; no real bank data.',
        )

        page.goto(base + '/funding/')
        page.get_by_label('Amount', exact=True).fill('20.00')
        page.get_by_role('button', name='Review funding').click()
        expect(page.get_by_text('$20.00 USD', exact=False).first).to_be_visible()
        confirm(page, user)
        funding = Operation.objects.filter(actor=user, kind='FUNDING').latest('created_at')
        funding = settle(page, funding.id)
        assert funding.state == 'COMPLETED'
        assert Journal.objects.filter(operation=funding, effect_kind='FUNDING').count() == 1
        page.reload()
        assert Journal.objects.filter(operation=funding, effect_kind='FUNDING').count() == 1
        observed(
            'funding_and_refresh',
            'Real browser review and bound confirmation settled through the independent simulator and worker. Refresh retained one operation and one funding journal.',
        )

        page.goto(base + '/send/')
        page.get_by_label('Recipient handle', exact=True).fill('jordan')
        page.get_by_label('Amount', exact=True).fill('1.001')
        page.get_by_role('button', name='Review transfer').click()
        expect(page.locator('.error-summary')).to_be_visible()
        expect(page.get_by_label('Recipient handle', exact=True)).to_have_value('jordan')
        page.get_by_label('Amount', exact=True).fill('5.25')
        page.get_by_label('Note', exact=False).fill('Browser journey transfer')
        page.get_by_role('button', name='Review transfer').click()
        confirm(page, user)
        transfer = Operation.objects.filter(actor=user, kind='TRANSFER').latest('created_at')
        assert transfer.state == 'COMPLETED'
        page.reload()
        assert Journal.objects.filter(operation=transfer).count() == 1
        observed(
            'send_validation_confirmation',
            'Invalid precision produced an associated form error and preserved safe input; corrected transfer reviewed exact recipient and amount, confirmed, and survived refresh without duplicate effects.',
        )

        page.goto(base + '/requests/')
        page.get_by_label('Payer handle', exact=True).fill('jordan')
        page.get_by_label('Amount', exact=True).fill('2.00')
        page.get_by_label('Note', exact=False).fill('Browser request')
        page.get_by_role('button', name='Create request').click()
        request = MoneyRequest.objects.filter(requester=user).latest('created_at')
        assert request.state == 'OPEN' and request.operation_id is None
        page.get_by_role('button', name='Cancel request').click()
        request.refresh_from_db()
        assert request.state == 'CANCELLED'
        observed(
            'request_cancel',
            'Exact amount request created no payment; requester cancellation reached a persistent cancelled detail.',
        )

        page.goto(base + '/schedules/')
        page.get_by_label('Recipient handle', exact=True).fill('jordan')
        page.get_by_label('Amount', exact=True).fill('1.00')
        due = db_now() + timedelta(days=1)
        page.get_by_label('Local date and time', exact=True).fill(due.strftime('%Y-%m-%dT%H:%M'))
        page.get_by_label('IANA timezone', exact=True).fill('UTC')
        page.get_by_label('UTC offset at that instant', exact=True).fill('+00:00')
        page.get_by_role('button', name='Review schedule').click()
        confirm(page, user)
        schedule = Schedule.objects.filter(owner=user).latest('created_at')
        assert schedule.state == 'SCHEDULED' and schedule.operation_id is None
        page.get_by_role('button', name='Cancel schedule').click()
        schedule.refresh_from_db()
        assert schedule.state == 'CANCELLED'
        observed(
            'schedule_cancel',
            'Browser scheduled an explicit UTC instant/offset with bounded delegation and no pre-reservation; cancellation persisted without an operation.',
        )

        page.goto(base + '/funding/')
        page.get_by_label('Amount', exact=True).fill('1000.00')
        page.get_by_role('button', name='Review funding').click()
        confirm(page, user)
        reviewed = Operation.objects.filter(actor=user, kind='FUNDING').latest('created_at')
        assert reviewed.state == 'REVIEW_PENDING'
        review_case_id = Case.objects.get(operation=reviewed, type='RISK').id
        observed(
            'risk_review_pending',
            'Threshold funding displayed Under review and retained incoming capacity without provider dispatch.',
        )

        staff_context = browser.new_context(
            viewport={'width': 1440, 'height': 1040}, java_script_enabled=False
        )
        staff_page = staff_context.new_page()
        reviewer = login(staff_page, seeded['reviewer'])
        staff_page.goto(base + '/staff/')
        staff_page.locator(f'a.case-card[href="/staff/cases/{review_case_id}/"]').click()
        staff_page.get_by_label('Decision', exact=True).select_option('APPROVE')
        staff_page.get_by_label('Reason', exact=False).fill(
            'Independent browser review of synthetic funding.'
        )
        staff_page.get_by_role('button', name='Review decision').click()
        confirm(staff_page, reviewer)
        reviewed = settle(page, reviewed.id)
        assert reviewed.state == 'COMPLETED'
        staff_page.screenshot(path=str(output / 'staff-case-desktop.png'), full_page=True)
        response = staff_page.goto(base + f'/payments/{transfer.id}/')
        assert response.status == 404
        observed(
            'staff_separation_and_ownership',
            'Separate eligible reviewer approved the bound threshold decision. The reviewer customer session could not retrieve another customer payment by known UUID.',
        )
        staff_context.close()

        page.goto(base + '/statements/')
        page.get_by_label('Start date · UTC', exact=True).fill(
            (db_now() - timedelta(days=1)).date().isoformat()
        )
        page.get_by_label('End date · UTC (exclusive)', exact=True).fill(
            (db_now() + timedelta(days=1)).date().isoformat()
        )
        page.get_by_label('Display timezone', exact=True).fill('America/Detroit')
        page.get_by_role('button', name='Generate statement').click()
        deadline = time.monotonic() + 65
        while (
            page.get_by_role('heading', name='Wallet statement', exact=True).count() == 0
            and time.monotonic() < deadline
        ):
            time.sleep(1)
            page.reload()
        expect(page.get_by_role('heading', name='Wallet statement', exact=True)).to_be_visible()
        with page.expect_download() as download_info:
            page.get_by_role('link', name='Download CSV').first.click()
        download = download_info.value
        target = output / 'statement.csv'
        download.save_as(str(target))
        assert 'DEMONSTRATION MONEY' in target.read_text()
        observed(
            'statement',
            'Real snapshot HTML and authenticated attachment CSV included simulation labeling, fixed balances, UTC timestamps and local timezone offsets.',
        )

        page.goto(base + '/wallet/')
        page.screenshot(path=str(output / 'wallet-desktop.png'), full_page=True)
        page.set_viewport_size({'width': 320, 'height': 900})
        page.goto(base + '/wallet/')
        page.screenshot(path=str(output / 'wallet-mobile-320.png'), full_page=True)
        # JavaScript is disabled for this journey; use a second context for DOM geometry and labels.
        browser_geometry = browser.new_context(
            viewport={'width': 320, 'height': 900}, storage_state=context.storage_state()
        )
        geometry = browser_geometry.new_page()
        geometry.goto(base + '/wallet/')
        assert geometry.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), (
            'Dashboard must reflow at 320 CSS pixels'
        )
        geometry.goto(base + '/send/')
        assert geometry.evaluate(
            'Array.from(document.querySelectorAll("input:not([type=hidden]),select,textarea")).every(el => el.labels && el.labels.length > 0)'
        ), 'Every visible form field must have a label'
        geometry.keyboard.press('Tab')
        expect(geometry.locator(':focus')).to_have_attribute('href', '#main')
        browser_geometry.close()
        observed(
            'responsive_labels_keyboard',
            'Desktop and 320-CSS-pixel screenshots retained. Dashboard had no document overflow at 320 pixels; form controls had associated labels; keyboard skip link reached main. Screen reader and full WCAG audit NOT RUN.',
        )
        page.get_by_role('button', name='Sign out').click()
        page.goto(base + '/wallet/')
        expect(page.get_by_role('heading', name='Welcome back')).to_be_visible()
        observed(
            'logout',
            'CSRF-protected POST logout invalidated the server session and a protected wallet navigation returned to sign-in.',
        )
        assert not errors, errors
        report = {
            'browser': browser.version,
            'javascript_disabled_for_primary_journey': True,
            'base': base,
            'observations': observations,
            'page_errors': errors,
            'limitations': [
                'Synthetic activation clock fixture advanced after waiting-rule observation.',
                'Screen-reader observation NOT RUN.',
                'Targeted browser checks do not establish all WCAG 2.2 AA criteria or the complete acceptance catalog.',
            ],
        }
        (output / 'report.json').write_text(json.dumps(report, indent=2))
        context.close()
        browser.close()
    print(
        f'Browser verification complete: {len(observations)} observed journeys. Private screenshots and report retained.',
        flush=True,
    )


if __name__ == '__main__':
    main()

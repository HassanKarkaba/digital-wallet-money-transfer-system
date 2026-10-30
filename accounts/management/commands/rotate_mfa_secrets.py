"""Reseal retained account seeds with the configured active key version."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import OperationalError, transaction

from accounts.models import SecurityMail, User
from accounts.services import SecurityError, decrypt_secret, encrypt_secret, lock_users
from audit.services import record


class Command(BaseCommand):
    help = 'Reseal MFA seeds under current key, verify plaintext equality, and audit. Keep old keys until every seed is checked.'

    def add_arguments(self, parser):
        parser.add_argument('--batch', type=int, default=100)
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        if settings.MODE != 'DEMO':
            raise CommandError('Key rotation is available only for this local DEMO environment.')
        batch = options['batch']
        if not 1 <= batch <= 1000:
            raise CommandError('Batch must be between one and one thousand.')
        prefix = settings.MFA_KEY_VERSION + ':'
        candidates = list(
            User.objects.exclude(mfa_secret='')
            .exclude(mfa_secret__startswith=prefix)
            .order_by('pk')
            .values_list('pk', flat=True)[:batch]
        )
        checked = 0
        try:
            for user_id in candidates:
                with transaction.atomic():
                    user = lock_users([user_id])[0]
                    if not user.mfa_secret or user.mfa_secret.startswith(prefix):
                        continue
                    plaintext = decrypt_secret(user.mfa_secret)
                    resealed = encrypt_secret(plaintext)
                    if decrypt_secret(resealed) != plaintext:
                        raise CommandError(
                            'Key rotation verification failed; the old ciphertext was retained.'
                        )
                    if not options['dry_run']:
                        old_version = user.mfa_secret.split(':', 1)[0]
                        user.mfa_secret = resealed
                        user.save(update_fields=['mfa_secret'])
                        record(
                            None,
                            'MFA_SEED_REENCRYPTED',
                            user.pk,
                            purpose='Explicit local key rotation',
                            metadata={'old_version': old_version, 'new_version': settings.MFA_KEY_VERSION},
                        )
                    checked += 1
            # Security mail shares the versioned envelope. Reseal it too so an
            # old key is not retired while a durable recovery link still needs it.
            mail_ids = list(
                SecurityMail.objects.exclude(encrypted_body__startswith=prefix)
                .order_by('pk')
                .values_list('pk', flat=True)[:batch]
            )
            mail_checked = 0
            for mail_id in mail_ids:
                preliminary = SecurityMail.objects.only('user_id').get(pk=mail_id)
                with transaction.atomic():
                    lock_users([preliminary.user_id])
                    mail = SecurityMail.objects.select_for_update().get(pk=mail_id)
                    if mail.encrypted_body.startswith(prefix):
                        continue
                    plaintext = decrypt_secret(mail.encrypted_body)
                    resealed = encrypt_secret(plaintext)
                    if decrypt_secret(resealed) != plaintext:
                        raise CommandError(
                            'Security mail key verification failed; old ciphertext was retained.'
                        )
                    if not options['dry_run']:
                        mail.encrypted_body = resealed
                        mail.save(update_fields=['encrypted_body'])
                        record(
                            None,
                            'SECURITY_MAIL_REENCRYPTED',
                            mail.pk,
                            purpose='Explicit local key rotation',
                            metadata={'new_version': settings.MFA_KEY_VERSION},
                        )
                    mail_checked += 1
        except OperationalError as exc:
            raise CommandError(
                'A bounded rotation step could not acquire its resources. Retain old keys and retry the same command.'
            ) from exc
        except SecurityError as exc:
            raise CommandError(
                'Key rotation stopped because a required decryption key is unavailable. Old keys must remain configured.'
            ) from exc
        remaining = User.objects.exclude(mfa_secret='').exclude(mfa_secret__startswith=prefix).count()
        mail_remaining = SecurityMail.objects.exclude(encrypted_body__startswith=prefix).count()
        action = 'Checked without mutation' if options['dry_run'] else 'Resealed and verified'
        self.stdout.write(
            f'{action}: {checked} MFA seeds and {mail_checked} security mail records. Old versions remaining: {remaining} seeds, {mail_remaining} mail records.'
        )
        if remaining or mail_remaining:
            self.stdout.write('Keep all old keys and repeat bounded batches before removing any key.')
        else:
            self.stdout.write(
                'All retained MFA seeds and encrypted security mail use the current key. Preserve verified backups and restart with the selected key ring.'
            )

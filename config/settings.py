import json
import os
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse

from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
MODE = os.environ.get('DWMT_MODE', 'DEMO')
if MODE != 'DEMO':
    raise ImproperlyConfigured('Only DEMO mode is supported.')


def required(name):
    value = os.environ.get(name)
    if not value:
        raise ImproperlyConfigured(f'Required runtime setting missing: {name}')
    return value


def database(name):
    parsed = urlparse(required(name))
    if parsed.hostname not in {'db', 'localhost', '127.0.0.1'}:
        raise ImproperlyConfigured('The supported local profile requires a local PostgreSQL binding.')
    if parsed.scheme not in ('postgres', 'postgresql'):
        raise ImproperlyConfigured('PostgreSQL is required.')
    return {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': parsed.path.lstrip('/'),
        'USER': unquote(parsed.username or ''),
        'PASSWORD': unquote(parsed.password or ''),
        'HOST': parsed.hostname,
        'PORT': parsed.port or 5432,
        'CONN_MAX_AGE': 0,
        'OPTIONS': {'connect_timeout': 2, 'options': '-c lock_timeout=2000 -c statement_timeout=5000'},
        'TEST': {'NAME': 'test_' + parsed.path.lstrip('/')},
    }


SECRET_KEY = required('DJANGO_SECRET_KEY')
DEBUG = False
ALLOWED_HOSTS = ['localhost', '127.0.0.1', 'testserver', 'web']
DATABASES = {'default': database('DATABASE_URL'), 'simulator': database('SIMULATOR_DATABASE_URL')}
if (
    DATABASES['default']['NAME'] == DATABASES['simulator']['NAME']
    or DATABASES['default']['USER'] == DATABASES['simulator']['USER']
):
    raise ImproperlyConfigured('Primary and simulator require separate databases and roles.')
DATABASE_ROUTERS = ['simulator.router.SimulatorRouter']
ENVIRONMENT_ID = required('ENVIRONMENT_ID')
RECOVERY_GENERATION = int(required('RECOVERY_GENERATION'))
MFA_KEY_VERSION = os.environ.get('MFA_KEY_VERSION', 'v1')
MFA_ENCRYPTION_KEYS = json.loads(required('MFA_ENCRYPTION_KEYS'))
CALLBACK_HMAC_KEY = required('CALLBACK_HMAC_KEY')
CALLBACK_KEY_ID = os.environ.get('CALLBACK_KEY_ID', 'v1')
CALLBACK_PREVIOUS_KEY_ID = os.environ.get('CALLBACK_PREVIOUS_KEY_ID')
CALLBACK_PREVIOUS_HMAC_KEY = os.environ.get('CALLBACK_PREVIOUS_HMAC_KEY')
CALLBACK_PREVIOUS_KEY_EXPIRES_AT = os.environ.get('CALLBACK_PREVIOUS_KEY_EXPIRES_AT')
if CALLBACK_PREVIOUS_KEY_EXPIRES_AT:
    from datetime import datetime, timedelta, timezone

    CALLBACK_PREVIOUS_KEY_EXPIRES_AT = datetime.fromisoformat(
        CALLBACK_PREVIOUS_KEY_EXPIRES_AT.replace('Z', '+00:00')
    )
    if CALLBACK_PREVIOUS_KEY_EXPIRES_AT.tzinfo is None or CALLBACK_PREVIOUS_KEY_EXPIRES_AT > datetime.now(
        timezone.utc
    ) + timedelta(minutes=10):
        raise ImproperlyConfigured(
            'Callback rotation overlap must be an explicit UTC instant at most ten minutes ahead.'
        )
    if (
        not CALLBACK_PREVIOUS_HMAC_KEY
        or not CALLBACK_PREVIOUS_KEY_ID
        or CALLBACK_PREVIOUS_KEY_ID == CALLBACK_KEY_ID
    ):
        raise ImproperlyConfigured('Callback previous key settings must be distinct and complete.')
PRIVATE_ROOT = Path(os.environ.get('PRIVATE_ROOT', '/private'))
EMAIL_FILE_PATH = os.environ.get('EMAIL_FILE_PATH', '/mail')
RECOVERY_MARKER_PATH = Path(os.environ.get('RECOVERY_MARKER_PATH', '/recovery/marker.json'))
EMAIL_BACKEND = 'django.core.mail.backends.filebased.EmailBackend'
DEFAULT_FROM_EMAIL = 'wallet@dwmt.test'
SITE_URL = 'http://localhost:8000'
ROOT_URLCONF = 'config.urls'
WSGI_APPLICATION = 'config.wsgi.application'
INSTALLED_APPS = [
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'config',
    'accounts',
    'audit',
    'ledger',
    'payments',
    'rails',
    'oversight',
    'reports',
    'notifications',
    'work',
    'simulator',
]
AUTH_USER_MODEL = 'accounts.User'
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'accounts.middleware.AccountSecurityMiddleware',
    'config.middleware.RequestBoundaryMiddleware',
]
TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'config.context_processors.wallet_context',
            ]
        },
    }
]
PASSWORD_HASHERS = [
    'django.contrib.auth.hashers.Argon2PasswordHasher',
    'django.contrib.auth.hashers.PBKDF2PasswordHasher',
]
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 15}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
]
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_AGE = 1800
SESSION_SAVE_EVERY_REQUEST = True
CSRF_COOKIE_SAMESITE = 'Lax'
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'
SECURE_REFERRER_POLICY = 'no-referrer'
LOGIN_URL = '/accounts/login/'
LOGIN_REDIRECT_URL = '/wallet/'
LOGOUT_REDIRECT_URL = '/'
PASSWORD_RESET_TIMEOUT = 1800
USE_TZ = True
TIME_ZONE = 'UTC'
LANGUAGE_CODE = 'en-us'
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'},
}
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
DATA_UPLOAD_MAX_MEMORY_SIZE = 32768
FILE_UPLOAD_MAX_MEMORY_SIZE = 0
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {'diagnostics': {'class': 'config.logging.DiagnosticHandler', 'level': 'WARNING'}},
    'loggers': {
        'django.request': {'handlers': ['diagnostics'], 'level': 'WARNING', 'propagate': False},
        'django.security': {'handlers': ['diagnostics'], 'level': 'WARNING', 'propagate': False},
    },
}

# Reject unsupported bindings before any protected activity is admitted.

try:
    ENVIRONMENT_ID = str(uuid.UUID(ENVIRONMENT_ID))
    if RECOVERY_GENERATION < 1 or len(SECRET_KEY) < 50 or len(CALLBACK_HMAC_KEY) < 32:
        raise ValueError('Secret or recovery configuration is invalid.')
    if MFA_KEY_VERSION not in MFA_ENCRYPTION_KEYS:
        raise ValueError('Active MFA key is absent.')
    for version, key in MFA_ENCRYPTION_KEYS.items():
        if not version or ':' in version or len(version) > 30:
            raise ValueError('Invalid key version.')
        Fernet(key)
except (TypeError, ValueError) as exc:
    raise ImproperlyConfigured('Invalid security or environment configuration.') from exc

role_mode = os.environ.get('DWMT_DATABASE_ROLE', 'runtime')
if role_mode not in {'runtime', 'migration'}:
    raise ImproperlyConfigured('Unsupported database role mode.')
for alias, prefix in [('default', 'wallet'), ('simulator', 'simulator')]:
    expected = prefix + ('_migrate' if role_mode == 'migration' else '_runtime')
    if DATABASES[alias]['USER'] != expected:
        raise ImproperlyConfigured(
            'Database credentials do not match the selected runtime or migration role.'
        )
for folder in [PRIVATE_ROOT, Path(EMAIL_FILE_PATH), RECOVERY_MARKER_PATH.parent]:
    resolved = folder.resolve()
    for exposed in [STATIC_ROOT.resolve(), (BASE_DIR / 'static').resolve()]:
        if resolved == exposed or exposed in resolved.parents:
            raise ImproperlyConfigured('Private storage must remain outside static assets.')
DATA_UPLOAD_MAX_NUMBER_FIELDS = 50

SESSION_COOKIE_NAME = 'dwmt_session_' + uuid.UUID(ENVIRONMENT_ID).hex
CSRF_COOKIE_NAME = 'dwmt_csrf_' + uuid.UUID(ENVIRONMENT_ID).hex

import base64
import json
import os
from pathlib import Path
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
env_file = BASE_DIR / '.env'
if env_file.exists():
    for line in env_file.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

DEBUG = os.environ.get('DJANGO_DEBUG', 'true').lower() == 'true'
LOCAL_ROOT = BASE_DIR / '.local'
local_secrets = {}
if DEBUG:
    LOCAL_ROOT.mkdir(mode=0o700, exist_ok=True)
    secret_path = LOCAL_ROOT / 'secrets.json'
    if not secret_path.exists():
        import secrets
        try:
            fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as handle:
                json.dump({'secret': secrets.token_urlsafe(64), 'document_key': base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()}, handle)
        except FileExistsError:
            pass
    local_secrets = json.loads(secret_path.read_text())
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', local_secrets.get('secret', ''))
DOCUMENT_ENCRYPTION_KEY = os.environ.get('DOCUMENT_ENCRYPTION_KEY', local_secrets.get('document_key', ''))
if len(SECRET_KEY) < 40 or not DOCUMENT_ENCRYPTION_KEY:
    raise ImproperlyConfigured('Set a strong DJANGO_SECRET_KEY and DOCUMENT_ENCRYPTION_KEY before production startup.')
try:
    if len(base64.urlsafe_b64decode(DOCUMENT_ENCRYPTION_KEY)) != 32:
        raise ValueError()
except Exception as exc:
    raise ImproperlyConfigured('DOCUMENT_ENCRYPTION_KEY must encode exactly 32 random bytes.') from exc

ALLOWED_HOSTS = [v.strip() for v in os.environ.get('DJANGO_ALLOWED_HOSTS', 'localhost,127.0.0.1,[::1],testserver' if DEBUG else '').split(',') if v.strip()]
if not DEBUG and not ALLOWED_HOSTS:
    raise ImproperlyConfigured('Set DJANGO_ALLOWED_HOSTS in production.')
INSTALLED_APPS = ['django.contrib.admin','django.contrib.auth','django.contrib.contenttypes','django.contrib.sessions','django.contrib.messages','django.contrib.staticfiles','portal']
MIDDLEWARE = ['django.middleware.security.SecurityMiddleware','whitenoise.middleware.WhiteNoiseMiddleware','django.contrib.sessions.middleware.SessionMiddleware','django.middleware.common.CommonMiddleware','django.middleware.csrf.CsrfViewMiddleware','django.contrib.auth.middleware.AuthenticationMiddleware','portal.middleware.PrivateResponseMiddleware','django.contrib.messages.middleware.MessageMiddleware','django.middleware.clickjacking.XFrameOptionsMiddleware']
ROOT_URLCONF = 'config.urls'
TEMPLATES = [{'BACKEND':'django.template.backends.django.DjangoTemplates','DIRS':[], 'APP_DIRS':True,'OPTIONS':{'context_processors':['django.template.context_processors.request','django.contrib.auth.context_processors.auth','django.contrib.messages.context_processors.messages','portal.context_processors.portal_context']}}]
WSGI_APPLICATION = 'config.wsgi.application'
database_url = os.environ.get('DATABASE_URL', '')
if database_url:
    from urllib.parse import urlsplit, unquote, parse_qs
    parsed = urlsplit(database_url)
    if parsed.scheme not in ('postgres', 'postgresql'):
        raise ImproperlyConfigured('DATABASE_URL must use PostgreSQL.')
    DATABASES = {'default': {'ENGINE':'django.db.backends.postgresql','NAME':unquote(parsed.path.lstrip('/')),'USER':unquote(parsed.username or ''),'PASSWORD':unquote(parsed.password or ''),'HOST':parsed.hostname,'PORT':parsed.port or 5432,'CONN_MAX_AGE':60,'OPTIONS':{k:v[0] for k,v in parse_qs(parsed.query).items() if k in ('sslmode',)}}}
else:
    DATABASES = {'default':{'ENGINE':'django.db.backends.sqlite3','NAME':os.environ.get('SQLITE_PATH', str(LOCAL_ROOT / 'portal.sqlite3')),'OPTIONS':{'timeout':20, 'transaction_mode':'IMMEDIATE'}}}
PRIVATE_STORAGE_ROOT = Path(os.environ.get('PRIVATE_STORAGE_ROOT', str(LOCAL_ROOT / 'documents')))
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 12 * 1024 * 1024
AUTH_PASSWORD_VALIDATORS = [{'NAME':'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},{'NAME':'django.contrib.auth.password_validation.MinimumLengthValidator','OPTIONS':{'min_length':12}},{'NAME':'django.contrib.auth.password_validation.CommonPasswordValidator'},{'NAME':'django.contrib.auth.password_validation.NumericPasswordValidator'}]
LANGUAGE_CODE = 'en-in'
TIME_ZONE = 'Asia/Kolkata'
USE_I18N = True
USE_TZ = True
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STORAGES = {'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'}}
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'portal:dashboard'
LOGOUT_REDIRECT_URL = 'login'
SESSION_COOKIE_AGE = 3600
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_SSL_REDIRECT = not DEBUG
SECURE_HSTS_SECONDS = 31536000 if not DEBUG else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
if os.environ.get('TRUST_PROXY_HTTPS','false').lower() == 'true':
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO','https')
X_FRAME_OPTIONS = 'DENY'
CSRF_TRUSTED_ORIGINS = [s for s in os.environ.get('CSRF_TRUSTED_ORIGINS','').split(',') if s]
DEMO_MODE = DEBUG and (LOCAL_ROOT / 'demo-mode').exists()
SMTP_ENABLED = os.environ.get('SMTP_ENABLED','false').lower() == 'true'
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend' if SMTP_ENABLED else 'django.core.mail.backends.locmem.EmailBackend'
EMAIL_HOST = os.environ.get('EMAIL_HOST','localhost')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT','587'))
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER','')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD','')
EMAIL_USE_TLS = os.environ.get('EMAIL_USE_TLS','true').lower() == 'true'
EMAIL_TIMEOUT = 15
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL','portal@example.invalid')
LOGGING = {'version':1,'disable_existing_loggers':False,'handlers':{'console':{'class':'logging.StreamHandler'}},'root':{'handlers':['console'],'level':'INFO'}}

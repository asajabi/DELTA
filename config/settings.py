"""
Django settings for config project.
"""

import os
import shutil
import sys
from pathlib import Path

from django.utils.translation import gettext_lazy as _

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv_if_present(path: Path) -> None:
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv_if_present(BASE_DIR / ".env")


# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/

def env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    raw = (os.getenv(name, str(default)) or "").strip()
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _sqlite_default_path() -> Path:
    explicit = (os.getenv("DJANGO_SQLITE_PATH", "") or "").strip()
    if explicit:
        return Path(explicit)

    local_appdata = (os.getenv("LOCALAPPDATA", "") or "").strip()
    if local_appdata:
        target_dir = Path(local_appdata) / "DELTA_POS"
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / "db.sqlite3"

        # One-time copy from legacy project DB so existing data is preserved.
        legacy_path = BASE_DIR / "db.sqlite3"
        if not target_path.exists() and legacy_path.exists():
            try:
                shutil.copy2(legacy_path, target_path)
            except OSError:
                pass
        return target_path

    return BASE_DIR / "db.sqlite3"


def _database_config():
    engine_aliases = {
        "sqlite": "django.db.backends.sqlite3",
        "sqlite3": "django.db.backends.sqlite3",
        "postgres": "django.db.backends.postgresql",
        "postgresql": "django.db.backends.postgresql",
        "mysql": "django.db.backends.mysql",
        "mariadb": "django.db.backends.mysql",
    }
    raw_engine = (os.getenv("DJANGO_DB_ENGINE", "django.db.backends.sqlite3") or "").strip()
    engine = engine_aliases.get(raw_engine.lower(), raw_engine)
    if not engine:
        engine = "django.db.backends.sqlite3"

    if engine == "django.db.backends.sqlite3":
        db_name = (os.getenv("DJANGO_DB_NAME", "") or "").strip()
        return {
            "ENGINE": engine,
            "NAME": db_name or _sqlite_default_path(),
            "OPTIONS": {
                "timeout": int(os.getenv("DJANGO_SQLITE_TIMEOUT_SECONDS", "20")),
            },
        }

    config = {
        "ENGINE": engine,
        "NAME": (os.getenv("DJANGO_DB_NAME", "") or "").strip(),
        "USER": (os.getenv("DJANGO_DB_USER", "") or "").strip(),
        "PASSWORD": os.getenv("DJANGO_DB_PASSWORD", ""),
        "HOST": (os.getenv("DJANGO_DB_HOST", "") or "").strip(),
        "PORT": (os.getenv("DJANGO_DB_PORT", "") or "").strip(),
    }

    if engine == "django.db.backends.mysql":
        mysql_collation = (os.getenv("DJANGO_DB_COLLATION", "utf8mb4_unicode_ci") or "").strip()
        config["OPTIONS"] = {
            "charset": "utf8mb4",
            "init_command": f"SET NAMES utf8mb4 COLLATE {mysql_collation}",
        }
        config["TEST"] = {
            "CHARSET": "utf8mb4",
            "COLLATION": mysql_collation,
        }
    elif engine == "django.db.backends.postgresql":
        config["OPTIONS"] = {
            "options": "-c client_encoding=UTF8",
        }

    return config


SECRET_KEY = os.getenv(
    "DJANGO_SECRET_KEY",
    "delta-pos-local-dev-change-this-5f16a29d7bd84cfca2d0f42b7f8f9f64",
)
DEBUG = env_bool("DJANGO_DEBUG", False)
IS_TESTING = any(arg == "test" or arg.startswith("test") for arg in sys.argv[1:])
SECURE_DEFAULTS = not DEBUG and not IS_TESTING

ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost,.ngrok-free.dev").split(",")
    if host.strip()
]
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "https://*.ngrok-free.dev").split(",")
    if origin.strip()
]


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    
    'inventory',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.locale.LocaleMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.template.context_processors.i18n',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'inventory.context_processors.nav_context',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'


# Database
# https://docs.djangoproject.com/en/5.2/ref/settings/#databases

DATABASES = {"default": _database_config()}


# Password validation
# https://docs.djangoproject.com/en/5.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/5.2/topics/i18n/

LANGUAGE_CODE = 'ar'
LANGUAGES = [
    ('ar', _('Arabic')),
    ('en', _('English')),
]
LOCALE_PATHS = [BASE_DIR / 'locale']
TIME_ZONE = 'Asia/Riyadh'

USE_I18N = True
USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.2/howto/static-files/

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'delta-cache',
    }
}

# Default primary key field type
# https://docs.djangoproject.com/en/5.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_URL = '/accounts/login/'
LOGIN_REDIRECT_URL = '/inventory/search/'
LOGOUT_REDIRECT_URL = '/accounts/login/'

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_SECURE = env_bool('DJANGO_SECURE_COOKIES', SECURE_DEFAULTS)
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SECURE = env_bool('DJANGO_SECURE_COOKIES', SECURE_DEFAULTS)
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'
SECURE_SSL_REDIRECT = env_bool('DJANGO_SECURE_SSL_REDIRECT', SECURE_DEFAULTS)
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_HSTS_SECONDS = env_int('DJANGO_SECURE_HSTS_SECONDS', 31536000 if SECURE_DEFAULTS else 0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool('DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS', SECURE_DEFAULTS)
SECURE_HSTS_PRELOAD = env_bool('DJANGO_SECURE_HSTS_PRELOAD', SECURE_DEFAULTS)

# Email (SMTP)
EMAIL_BACKEND = os.getenv("DJANGO_EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend").strip()
EMAIL_HOST = os.getenv("DJANGO_EMAIL_HOST", "").strip()
EMAIL_PORT = int(os.getenv("DJANGO_EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.getenv("DJANGO_EMAIL_HOST_USER", "").strip()
EMAIL_HOST_PASSWORD = os.getenv("DJANGO_EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("DJANGO_EMAIL_USE_TLS", True)
EMAIL_USE_SSL = env_bool("DJANGO_EMAIL_USE_SSL", False)
DEFAULT_FROM_EMAIL = os.getenv("DJANGO_DEFAULT_FROM_EMAIL", EMAIL_HOST_USER or "no-reply@delta.local").strip()
SERVER_EMAIL = os.getenv("DJANGO_SERVER_EMAIL", DEFAULT_FROM_EMAIL).strip()

# In local debug without SMTP config, print emails to console to avoid silent confusion.
if DEBUG and EMAIL_BACKEND == "django.core.mail.backends.smtp.EmailBackend" and not EMAIL_HOST:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': os.getenv('DJANGO_LOG_LEVEL', 'INFO'),
    },
    'loggers': {
        'django.request': {
            'handlers': ['console'],
            'level': 'WARNING',
            'propagate': False,
        },
        'inventory': {
            'handlers': ['console'],
            'level': os.getenv('DJANGO_LOG_LEVEL', 'INFO'),
            'propagate': False,
        },
    },
}

# SMACC webhook controls (defense in depth for public endpoints)
SMACC_WEBHOOK_IP_ALLOWLIST = [
    ip.strip()
    for ip in os.getenv("SMACC_WEBHOOK_IP_ALLOWLIST", "").split(",")
    if ip.strip()
]
SMACC_WEBHOOK_RATE_LIMIT_PER_MINUTE = int(os.getenv("SMACC_WEBHOOK_RATE_LIMIT_PER_MINUTE", "120"))
SMACC_WEBHOOK_SIGNATURE_HEADER = os.getenv("SMACC_WEBHOOK_SIGNATURE_HEADER", "X-Smacc-Signature")

# Real AI assistant integration (used by /inventory/assistant/ and /inventory/chat/)
AI_ASSISTANT_ENABLED = env_bool("AI_ASSISTANT_ENABLED", True)
AI_ASSISTANT_PROVIDER = (os.getenv("AI_ASSISTANT_PROVIDER", "openai") or "openai").strip().lower()
AI_ASSISTANT_MODEL = (os.getenv("AI_ASSISTANT_MODEL", "gpt-4.1-mini") or "gpt-4.1-mini").strip()
AI_ASSISTANT_API_KEY = (os.getenv("AI_ASSISTANT_API_KEY", "") or "").strip()
AI_ASSISTANT_BASE_URL = (os.getenv("AI_ASSISTANT_BASE_URL", "https://api.openai.com/v1") or "https://api.openai.com/v1").strip()
AI_ASSISTANT_TIMEOUT_SECONDS = int(os.getenv("AI_ASSISTANT_TIMEOUT_SECONDS", "20"))

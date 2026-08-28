"""
Django settings for acm project.
"""
import os
from datetime import timedelta
from pathlib import Path
import environ

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env()
ENV_FILE = BASE_DIR / ".env"
if ENV_FILE.exists():
    environ.Env.read_env(str(ENV_FILE))
else:
    print(f"[settings] WARNING: .env not found at {ENV_FILE}")

# Quick-start development settings - unsuitable for production
SECRET_KEY = env(
    'SECRET_KEY', default='django-insecure-wci8i*h_0lxjng1cw!mi0taj4v)h2z=b03-a6s8pliv573m!bd')
DEBUG = False

ALLOWED_HOSTS = ['*']
CSRF_TRUSTED_ORIGINS = [
    "https://aut-icpc.ir",
    "https://www.aut-icpc.ir",
    "http://localhost:8000",
    "https://github.com",
]

# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    "drf_spectacular",
    "drf_spectacular_sidecar",
    'rest_framework',
    'django_filters',

    # project apps
    'accounts',
    'notification',
    'django_celery_results',
    'presentations',
    'competitions',
    'payment',

    # storage / auth helpers
    'storages',
    'whitenoise',
    'rest_framework_simplejwt.token_blacklist',
]

ZARINPAL_MERCHANT_ID = env("ZARINPAL_MERCHANT_ID",
                           default="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")
PAYMENT_CALLBACK_BASE = env(
    "PAYMENT_CALLBACK_BASE", default="https://your-site.com/api/payment/callback/")
PAYMENT_FRONTEND_RETURN = env(
    "PAYMENT_FRONTEND_RETURN", default="https://your-frontend.example/payresult")

CELERY_BROKER_URL = env("CELERY_BROKER_URL",
                        default="redis://127.0.0.1:6379/2")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND",
                            default="redis://127.0.0.1:6379/3")
CELERY_TASK_ALWAYS_EAGER = False  # set True for local dev if you want sync
CELERY_TASK_TIME_LIMIT = 30
CELERY_TASK_SOFT_TIME_LIMIT = 25

# Notification throughput
NOTIF_EMAIL_RATE = env("NOTIF_EMAIL_RATE", default="30/m")
NOTIF_BULK_CHUNK_SIZE = env.int("NOTIF_BULK_CHUNK_SIZE", default=100)
NOTIF_BULK_RETRY_MAX = env.int("NOTIF_BULK_RETRY_MAX", default=5)
NOTIF_BULK_RETRY_BACKOFF = env.int("NOTIF_BULK_RETRY_BACKOFF", default=300)

# Email (SMTP/Gmail)
EMAIL_PROVIDER = env("EMAIL_PROVIDER", default="smtp")
EMAIL_HOST = env("EMAIL_HOST", default="smtp.gmail.com")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="your@gmail.com")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD",
                          default="app-password-or-secret")
EMAIL_USE_SSL = env.bool("EMAIL_USE_SSL", default=True)
EMAIL_DEFAULT_FROM = env("EMAIL_DEFAULT_FROM",
                         default="ACM <no-reply@yourdomain>")

AUTH_USER_MODEL = "accounts.User"

REST_FRAMEWORK = {
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticatedOrReadOnly",
    ],
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "EXCEPTION_HANDLER": "acm.exceptions.custom_exception_handler",
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=15),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=30),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "SIGNING_KEY": SECRET_KEY,
    "ALGORITHM": "HS256",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "ACM API",
    "DESCRIPTION": "Authentication & user API",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SECURITY": [{"BearerAuth": []}],
    "COMPONENT_SPLIT_REQUEST": True,
    "COMPONENT_NO_READ_ONLY_REQUIRED": True,
}

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": env("REDIS_URL", default="redis://127.0.0.1:6379/3"),
        "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
        "TIMEOUT": 60 * 60,
    }
}

OTP_SECRET = env("OTP_SECRET", default="change-me-super-secret")

# Refresh cookie shared across all auth flows
REFRESH_TOKEN_COOKIE = {
    "key": "refresh_token",
    "httponly": True,
    "secure": True,
    "samesite": "Lax",
    "path": "/api/accounts/",
    "domain": ".aut-icpc.ir",
}

ROOT_URLCONF = 'acm.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'acm.wsgi.application'

# Database
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": os.environ["MYSQL_DATABASE"],
        "USER": os.environ["MYSQL_USER"],
        "PASSWORD": os.environ["MYSQL_PASSWORD"],
        "HOST": os.getenv("MYSQL_HOST", "db"),
        "PORT": os.getenv("MYSQL_PORT", "3306"),
        "OPTIONS": {"charset": "utf8mb4"},
    }
}

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator', },
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', },
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator', },
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator', },
]

# i18n
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Tehran'
USE_I18N = True
USE_TZ = True

# S3 / static/media
LIARA_ENDPOINT = os.getenv("AWS_S3_ENDPOINT_URL",
                           default="BUCKET_ENDPOINT_DEFAULT")
LIARA_BUCKET_NAME = os.getenv(
    "AWS_STORAGE_BUCKET_NAME", default="BUCKET_NAME_DEFAULT")
LIARA_ACCESS_KEY = os.getenv(
    "AWS_ACCESS_KEY_ID", default="BUCKET_ACCESS_KEY_DEFAULT")
LIARA_SECRET_KEY = os.getenv(
    "AWS_SECRET_ACCESS_KEY", default="BUCKET_SECRET_KEY_DEFAULT")

AWS_ACCESS_KEY_ID = LIARA_ACCESS_KEY
AWS_SECRET_ACCESS_KEY = LIARA_SECRET_KEY
AWS_STORAGE_BUCKET_NAME = LIARA_BUCKET_NAME
AWS_S3_ENDPOINT_URL = LIARA_ENDPOINT
AWS_S3_REGION_NAME = 'us-east-1'

STORAGES = {
    "default": {  # media
        "BACKEND": "storages.backends.s3.S3Storage",
    },
    "staticfiles": {  # static served by whitenoise in container
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

AWS_QUERYSTRING_AUTH = False

STATIC_ROOT = BASE_DIR / 'static'
STATIC_URL = '/static/'
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

MEDIA_ROOT = BASE_DIR / 'media'
MEDIA_URL = 'media/'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# --- GitHub OAuth (already present – kept as-is) ---
GITHUB_CLIENT_ID = env("GITHUB_CLIENT_ID")
GITHUB_CLIENT_SECRET = env("GITHUB_CLIENT_SECRET")
GITHUB_REDIRECT_URI = env(
    "GITHUB_REDIRECT_URI",
    default="https://aut-icpc.ir/api/accounts/github/callback/",
)

# --- Frontend redirect after social login (both GitHub & Codeforces) ---
FRONTEND_LOGIN_REDIRECT = env(
    "FRONTEND_LOGIN_REDIRECT",
    default="https://aut-icpc.ir/?login=ok",
)

# --- Shared OAuth cookies (state/nonce/PKCE) for ALL providers ---
# Use a common path so they work for /api/accounts/github/* and /api/accounts/cf/*
OAUTH_STATE_COOKIE = {
    "key": "oauth_state",
    "max_age": 600,
    "httponly": True,
    "secure": True,
    "samesite": "Lax",
    "path": "/api/accounts/",
    "domain": ".aut-icpc.ir",
}
OAUTH_NONCE_COOKIE = {
    "key": "oauth_nonce",
    "max_age": 600,
    "httponly": True,
    "secure": True,
    "samesite": "Lax",
    "path": "/api/accounts/",
    "domain": ".aut-icpc.ir",
}
OAUTH_PKCE_COOKIE = {
    "key": "oauth_pkce",
    "max_age": 600,
    "httponly": True,
    "secure": True,
    "samesite": "Lax",
    "path": "/api/accounts/",
    "domain": ".aut-icpc.ir",
}

# --- Codeforces OIDC (new) ---
CODEFORCES_OIDC_ISSUER = "https://codeforces.com"
CODEFORCES_OIDC_DISCOVERY_URL = "https://codeforces.com/.well-known/openid-configuration"
CODEFORCES_CLIENT_ID = env("CODEFORCES_CLIENT_ID", default="")
CODEFORCES_CLIENT_SECRET = env("CODEFORCES_CLIENT_SECRET", default="")
CODEFORCES_REDIRECT_URI = "https://aut-icpc.ir/api/accounts/codeforces/callback/"

# Skyroom
SKYROOM_BASEURL = env("SKYROOM_BASEURL", default="")
SKYROOM_APIKEY = env("SKYROOM_APIKEY", default="")
SKYROOM_ROOMID = env("SKYROOM_ROOMID", default="")

# Competition
COMPETITION_APPROVAL_REDIRECT_URL = env(
    "COMPETITION_APPROVAL_REDIRECT_URL", default="https://aut-icpc.ir/DAMN")
PAYMENT_EMAIL_LINK_BASE_URL = env("PAYMENT_EMAIL_LINK_BASE_URL", default="")

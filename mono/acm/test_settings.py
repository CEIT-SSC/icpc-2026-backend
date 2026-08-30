"""Self-contained settings for the repository test suite."""

import os


for key in ("MYSQL_DATABASE", "MYSQL_USER", "MYSQL_PASSWORD"):
    os.environ.setdefault(key, "test")
os.environ.setdefault("GITHUB_CLIENT_ID", "test")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test")

from .settings import *  # noqa: E402,F403


DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
CELERY_TASK_ALWAYS_EAGER = True
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

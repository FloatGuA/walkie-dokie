"""Django settings for the local contract-administration web process."""

from __future__ import annotations

import os
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
VAR_ROOT = REPOSITORY_ROOT / "var" / "contract-intelligence"
VAR_ROOT.mkdir(parents=True, exist_ok=True)

SECRET_KEY = os.getenv("CONTRACT_ADMIN_SECRET_KEY", "local-development-only-secret")
DEBUG = os.getenv("CONTRACT_ADMIN_DEBUG", "1") == "1"
ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv("CONTRACT_ADMIN_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")
    if host.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "walkie_dokie.contract_intelligence.apps.ContractIntelligenceConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "walkie_dokie.contract_admin.urls"
WSGI_APPLICATION = "walkie_dokie.contract_admin.wsgi.application"
ASGI_APPLICATION = "walkie_dokie.contract_admin.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [REPOSITORY_ROOT / "src" / "walkie_dokie" / "contract_admin" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

if os.getenv("CONTRACT_DB_ENGINE", "sqlite") == "postgresql":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ["CONTRACT_DB_NAME"],
            "USER": os.environ["CONTRACT_DB_USER"],
            "PASSWORD": os.environ["CONTRACT_DB_PASSWORD"],
            "HOST": os.getenv("CONTRACT_DB_HOST", "127.0.0.1"),
            "PORT": os.getenv("CONTRACT_DB_PORT", "5432"),
            "CONN_MAX_AGE": 60,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": Path(os.getenv("CONTRACT_SQLITE_PATH", VAR_ROOT / "admin.db")),
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
MEDIA_URL = "media/"
MEDIA_ROOT = Path(os.getenv("CONTRACT_MEDIA_ROOT", VAR_ROOT / "files"))
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
FILE_UPLOAD_PERMISSIONS = 0o640
DATA_UPLOAD_MAX_MEMORY_SIZE = 110 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024

LOGIN_URL = "/admin/login/"
X_FRAME_OPTIONS = "DENY"

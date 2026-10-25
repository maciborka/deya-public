import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY or SECRET_KEY == "change-me":
    raise ImproperlyConfigured("DJANGO_SECRET_KEY не задан или равен плейсхолдеру из .env.example")
DEBUG = os.environ.get("DJANGO_DEBUG", "") == "1"
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]
# Внешний доступ через Cloudflare Tunnel: https-origin для CSRF, схема из заголовка прокси
CSRF_TRUSTED_ORIGINS = [o.strip() for o in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()]
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

INSTALLED_APPS = [
    "unfold",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "deya_admin.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "deya_admin.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "deya"),
        "USER": os.environ.get("POSTGRES_USER", "deya"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
        "HOST": os.environ.get("POSTGRES_HOST", "db"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "ru"
TIME_ZONE = "Europe/Kyiv"
USE_I18N = True
USE_TZ = True

# под /admin/, чтобы проходило через маршрут туннеля с path ^/admin/
STATIC_URL = "admin/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

UNFOLD = {
    "SITE_TITLE": "Deya",
    "SITE_HEADER": "Deya — инвертор Deye 12 кВт",
    "SITE_SYMBOL": "solar_power",
    "DASHBOARD_CALLBACK": "core.overview.dashboard_callback",
    "SIDEBAR": {
        "show_search": True,
        "navigation": [
            {
                "title": "Управление",
                "items": [
                    {"title": "Режим инвертора", "icon": "tune", "link": "/admin/core/strategysettings/mode/"},
                    {"title": "Записи в инвертор", "icon": "edit_note", "link": "/admin/core/inverterwriterequest/"},
                    {"title": "Настройки стратегии", "icon": "battery_charging_full", "link": "/admin/core/strategysettings/"},
                    {"title": "Решения на ночь", "icon": "event_note", "link": "/admin/core/chargedecision/"},
                ],
            },
            {
                "title": "Прогноз",
                "items": [
                    {"title": "Провайдеры прогноза", "icon": "cloud", "link": "/admin/core/forecastprovider/"},
                    {"title": "Панели", "icon": "solar_power", "link": "/admin/core/panelgroup/"},
                ],
            },
            {
                "title": "Тарифы",
                "items": [
                    {"title": "Тарифные планы", "icon": "payments", "link": "/admin/core/tariffplan/"},
                ],
            },
            {
                "title": "Данные",
                "items": [
                    {"title": "Телеметрия", "icon": "monitoring", "link": "/admin/core/sample/"},
                    {"title": "Снимки настроек", "icon": "settings_backup_restore", "link": "/admin/core/settingssnapshot/"},
                    {"title": "Ошибки опроса", "icon": "error", "link": "/admin/core/pollerror/"},
                    {"title": "Прогноз (сырые)", "icon": "query_stats", "link": "/admin/core/forecastraw/"},
                    {"title": "Запуски прогноза", "icon": "play_circle", "link": "/admin/core/forecastrun/"},
                ],
            },
            {
                "title": "Доступ",
                "items": [
                    {"title": "Пользователи", "icon": "person", "link": "/admin/auth/user/"},
                    {"title": "Группы", "icon": "group", "link": "/admin/auth/group/"},
                ],
            },
        ],
    },
}

import os
from datetime import timedelta
from pathlib import Path
import environ

# Base directory
BASE_DIR = Path(__file__).resolve().parent.parent

# Initialize environment variables
env = environ.Env(
    DEBUG=(bool, True),
    SECRET_KEY=(str, "django-insecure-avestellar-finance-development-key-default-32chars"),
    ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1", "0.0.0.0"]),
    DEFAULT_CURRENCY=(str, "KES"),
    DEFAULT_DECIMAL_PLACES=(int, 2),
    AI_ASSISTANT_ENABLED=(bool, False),
    MAUZOPILOT_ENABLED=(bool, False),
)

# Read .env file if it exists
env_file = BASE_DIR / ".env"
if env_file.exists():
    environ.Env.read_env(env_file)

SECRET_KEY = env("SECRET_KEY")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

# Application definition
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party apps
    "rest_framework",
    "rest_framework_simplejwt",
    "django_filters",
    "drf_spectacular",
    "corsheaders",
    # Project apps (Modular monolith)
    "identity.apps.IdentityConfig",
    "organizations.apps.OrganizationsConfig",
    "auditlog.apps.AuditlogConfig",
    "ledger.apps.LedgerConfig",
    "invoicing.apps.InvoicingConfig",
    "purchases.apps.PurchasesConfig",
    "payments.apps.PaymentsConfig",
    "expenses.apps.ExpensesConfig",
    "tax.apps.TaxConfig",
    "inventory.apps.InventoryConfig",
    "automation.apps.AutomationConfig",
    "integrations.apps.IntegrationsConfig",
    "analytics.apps.AnalyticsConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "organizations.middleware.OrganizationContextMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# Custom User Model
AUTH_USER_MODEL = "identity.User"

# Database Configuration
# Primary: PostgreSQL. Fallback: SQLite
if "DATABASE_URL" in os.environ or env.str("DATABASE_URL", default=""):
    DATABASES = {
        "default": env.db("DATABASE_URL")
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Internationalization
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Django REST Framework
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_FILTER_BACKENDS": (
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ),
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

# SimpleJWT Settings
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(
        minutes=env.int("JWT_ACCESS_TOKEN_LIFETIME_MINUTES", default=60)
    ),
    "REFRESH_TOKEN_LIFETIME": timedelta(
        days=env.int("JWT_REFRESH_TOKEN_LIFETIME_DAYS", default=7)
    ),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": False,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

# OpenAPI / Swagger via drf-spectacular
SPECTACULAR_SETTINGS = {
    "TITLE": "AVESTELLAR FINANCE API",
    "DESCRIPTION": (
        "API-first accounting and financial intelligence platform for businesses, "
        "finance teams, and growing enterprises. Provides double-entry general ledger, "
        "invoicing, payments, transactional outbox, MauzoPilot adapter, and n8n webhooks."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "SCHEMA_PATH_PREFIX": r"/api/v[0-9]+",
    "ENUM_NAME_OVERRIDES": {
        "PeriodStatusEnum": "ledger.models.PeriodStatus",
        "JournalStatusEnum": "ledger.models.JournalStatus",
        "InvoiceStatusEnum": "invoicing.models.InvoiceStatus",
        "BillStatusEnum": "purchases.models.BillStatus",
        "PaymentStatusEnum": "payments.models.PaymentStatus",
    },
}

# Financial Core Engine Settings
DEFAULT_CURRENCY = env("DEFAULT_CURRENCY")
DEFAULT_DECIMAL_PLACES = env("DEFAULT_DECIMAL_PLACES")

# MauzoPilot Connector Configuration
MAUZOPILOT_ENABLED = env("MAUZOPILOT_ENABLED")
MAUZOPILOT_BASE_URL = env.str("MAUZOPILOT_BASE_URL", default="https://api.mauzopilot.com")
MAUZOPILOT_API_KEY = env.str("MAUZOPILOT_API_KEY", default="")
MAUZOPILOT_WEBHOOK_SECRET = env.str("MAUZOPILOT_WEBHOOK_SECRET", default="")

# Webhook Subsystem
WEBHOOK_DEFAULT_TIMEOUT_SECONDS = env.int("WEBHOOK_DEFAULT_TIMEOUT_SECONDS", default=10)
WEBHOOK_MAX_RETRIES = env.int("WEBHOOK_MAX_RETRIES", default=5)

# AI Engine Settings
AI_ASSISTANT_ENABLED = env("AI_ASSISTANT_ENABLED")
AI_ASSISTANT_PROVIDER = env.str("AI_ASSISTANT_PROVIDER", default="none")
AI_ASSISTANT_API_KEY = env.str("AI_ASSISTANT_API_KEY", default="")

# CORS settings
CORS_ALLOW_ALL_ORIGINS = DEBUG

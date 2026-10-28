from datetime import timedelta
from pathlib import Path

import environ
from django.core.exceptions import ImproperlyConfigured

from config.env_validation import (
    resolve_environment,
    strict_mode,
    validate_allowed_hosts,
    validate_cache_url,
    validate_database_url,
    validate_effective_database_engine,
    validate_origin_configuration,
    validate_secret_key,
)

# Base directory
BASE_DIR = Path(__file__).resolve().parent.parent

# Initialize environment variables. DEBUG defaults to False so that a missing or
# mis-set configuration fails closed (deployed behaviour) instead of open.
environ_env_defaults = {
    "DEBUG": (bool, False),
    "ALLOWED_HOSTS": (list, []),
    "DEFAULT_CURRENCY": (str, "KES"),
    "DEFAULT_DECIMAL_PLACES": (int, 2),
    "AI_ASSISTANT_ENABLED": (bool, False),
    "MAUZOPILOT_ENABLED": (bool, False),
}
env = environ.Env(**environ_env_defaults)

# Read .env file if it exists
env_file = BASE_DIR / ".env"
if env_file.exists():
    environ.Env.read_env(env_file)

DEBUG = env("DEBUG")

# DJANGO_ENV selects the deployment profile. ENVIRONMENT is accepted as a legacy
# alias so existing local .env files keep working; an unknown value is rejected
# rather than guessed (see config/env_validation.py).
DJANGO_ENV = resolve_environment(
    env.str("DJANGO_ENV", default=""), env.str("ENVIRONMENT", default="")
)

# "Strict" mode is any deployed run: staging/production, or simply DEBUG=False.
# Local development and the automated test suite run with DJANGO_ENV=development
# and DEBUG=True (see .env), so they keep their ergonomic defaults and stay
# usable while anything deployed fails closed. There is no built-in SECRET_KEY
# fallback: every profile must supply one.
STRICT_CONFIG = strict_mode(DJANGO_ENV, DEBUG)

SECRET_KEY = validate_secret_key(env.str("SECRET_KEY", default=""), STRICT_CONFIG)

ALLOWED_HOSTS = validate_allowed_hosts(
    env("ALLOWED_HOSTS") or ([] if STRICT_CONFIG else ["localhost", "127.0.0.1", "0.0.0.0"]),
    STRICT_CONFIG,
)

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
    "rest_framework_simplejwt.token_blacklist",
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
    "whitenoise.middleware.WhiteNoiseMiddleware",
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

# Database Configuration.
# Deployed configurations MUST use a real PostgreSQL DATABASE_URL and must never
# silently fall back to the on-disk SQLite file (a data-durability hazard for a
# financial ledger). Local development may still use SQLite for convenience.
_database_url = validate_database_url(
    env.str("DATABASE_URL", default=""), STRICT_CONFIG
)

if _database_url:
    try:
        DATABASES = {"default": env.db("DATABASE_URL")}
    except ImproperlyConfigured:
        raise
    except Exception:
        # The message never echoes the URL: it may embed a password.
        raise ImproperlyConfigured(
            "DATABASE_URL could not be parsed as a database URL."
        ) from None
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

# Enforce PostgreSQL based on the *resolved* engine, not merely on whether a
# DATABASE_URL variable happened to be present.
validate_effective_database_engine(DATABASES["default"]["ENGINE"], STRICT_CONFIG)

# Intentional connection handling. CONN_MAX_AGE is opt-in via env so the
# deployer decides based on whether a PgBouncer transaction pooler sits in
# front (see docs/staging_deployment.md). The safe default of 0 keeps the app
# compatible with transaction pooling; CONN_HEALTH_CHECKS avoids stale conns.
if DATABASES["default"]["ENGINE"] == "django.db.backends.postgresql":
    DATABASES["default"]["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=0)
    DATABASES["default"]["CONN_HEALTH_CHECKS"] = True
    _db_options = DATABASES["default"].setdefault("OPTIONS", {})
    _db_options.setdefault(
        "connect_timeout", env.int("DB_CONNECT_TIMEOUT_SECONDS", default=10)
    )

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
# WhiteNoise serves collected static assets (Django admin, etc.) from the app
# process, so staging needs no separate static file server. Compressed + content
# -hashed manifests are used only outside DEBUG to keep local reloads simple.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "whitenoise.storage.CompressedManifestStaticFilesStorage"
            if not DEBUG
            else "django.contrib.staticfiles.storage.StaticFilesStorage"
        )
    },
}

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
    # Per-scope throttling is applied on the sensitive unauthenticated views
    # (login/register). We do NOT throttle every authenticated endpoint by
    # default. Correct multi-worker rate limiting requires REDIS_URL (see the
    # CACHES block below); LocMemCache is per-process only.
    "DEFAULT_THROTTLE_CLASSES": (),
    "DEFAULT_THROTTLE_RATES": {
        "login": env.str("THROTTLE_LOGIN_RATE", default="20/min"),
        "register": env.str("THROTTLE_REGISTER_RATE", default="10/hour"),
    },
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
    # Rotate AND revoke: a stolen refresh token is blacklisted on use so it
    # cannot be replayed after a legitimate rotation. Requires the
    # token_blacklist app (added to INSTALLED_APPS) and its migrations.
    "BLACKLIST_AFTER_ROTATION": True,
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

# --- CORS / CSRF: explicit and never derived from DEBUG ---------------------
# The Next.js app proxies /api/v1 same-origin, so cross-origin access is only
# needed when the browser calls the API directly (NEXT_PUBLIC_API_BASE_URL).
# Enable it by listing exact origins; there is deliberately no wildcard and it is
# NEVER switched on just because DEBUG is true.
CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[])
CORS_ALLOW_CREDENTIALS = bool(CORS_ALLOWED_ORIGINS)
CORS_ALLOW_ALL_ORIGINS = env.bool("CORS_ALLOW_ALL_ORIGINS", default=False)
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])
# Escape hatch for a narrowly justified case (documented in the deployment
# notes). Deployed configurations otherwise refuse wildcard/permissive origins.
ALLOW_PERMISSIVE_CORS = env.bool("ALLOW_PERMISSIVE_CORS", default=False)
validate_origin_configuration(
    allow_all=CORS_ALLOW_ALL_ORIGINS,
    allowed_origins=CORS_ALLOWED_ORIGINS,
    csrf_trusted_origins=CSRF_TRUSTED_ORIGINS,
    strict=STRICT_CONFIG,
    allow_permissive_override=ALLOW_PERMISSIVE_CORS,
)

# --- HTTPS / security headers (applied when not in local DEBUG) -------------
SECURE_PROXY_SSL_HEADER = None
if not DEBUG:
    SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
    SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=31536000)  # 1 year
    SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool(
        "SECURE_HSTS_INCLUDE_SUBDOMAINS", default=True
    )
    SECURE_HSTS_PRELOAD = env.bool("SECURE_HSTS_PRELOAD", default=False)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"
    X_FRAME_OPTIONS = "DENY"
    # Behind a TLS-terminating proxy that sets X-Forwarded-Proto (the common
    # managed-PaaS topology), trust it so redirect/secure-cookie behave correctly.
    if env.bool("USE_X_FORWARDED_PROTO", default=True):
        SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# --- Request / upload size ceilings -----------------------------------------
# Explicit, documented limits for JSON bodies and uploaded statement files.
# The multipart FILE part is spooled to temp above FILE_UPLOAD_MAX_MEMORY_SIZE;
# DATA_UPLOAD_MAX_MEMORY_SIZE bounds the non-file request body. Keep these
# consistent with the statement-import serializer's own size guard.
DATA_UPLOAD_MAX_MEMORY_SIZE = env.int(
    "DATA_UPLOAD_MAX_MEMORY_SIZE", default=10 * 1024 * 1024
)  # 10 MiB
FILE_UPLOAD_MAX_MEMORY_SIZE = env.int(
    "FILE_UPLOAD_MAX_MEMORY_SIZE", default=5 * 1024 * 1024
)  # 5 MiB
# Largest statement file accepted by the reconciliation import endpoint. Matches
# the 15 MB browser-side guard in frontend/src/components/statements.tsx; the
# serializer enforces it server-side as well. Files above
# FILE_UPLOAD_MAX_MEMORY_SIZE are spooled to disk rather than held in memory, so
# this ceiling does not raise the process memory footprint by the same amount.
# The reverse proxy must allow at least this body size (see deployment notes).
MAX_STATEMENT_UPLOAD_BYTES = env.int(
    "MAX_STATEMENT_UPLOAD_BYTES", default=15 * 1024 * 1024
)  # 15 MiB

# --- Cache (throttling state) -----------------------------------------------
# Authentication throttling (ScopedRateThrottle) keeps its counters in the
# default cache. With no cache URL the process falls back to LocMemCache: that is
# correct for a single dev/test process but is NOT shared between Gunicorn
# workers, so per-process throttling must never be described as distributed rate
# limiting. Set REDIS_URL (rediss:// for anything off-host) to share the state.
#
# The built-in Redis backend propagates connection errors instead of swallowing
# them, so an unreachable shared cache makes throttled endpoints fail closed
# (HTTP 5xx) rather than silently allowing unlimited attempts. Verify
# connectivity with scripts/check_shared_cache.py; a configured URL is a claim
# that must be proved before it is reported as distributed throttling.
REDIS_URL = validate_cache_url(
    env.str("REDIS_URL", default=""),
    strict=STRICT_CONFIG,
    require_shared=env.bool("CACHE_REQUIRE_SHARED", default=False),
    allow_insecure_transport=env.bool("ALLOW_INSECURE_CACHE_TRANSPORT", default=False),
)
# Reporting flags: configuration only, not proof of reachability.
SHARED_CACHE_CONFIGURED = bool(REDIS_URL)
SHARED_CACHE_USES_TLS = REDIS_URL.startswith("rediss://")
CACHE_SOCKET_TIMEOUT_SECONDS = env.int("CACHE_SOCKET_TIMEOUT_SECONDS", default=3)
if REDIS_URL:
    # Django's redis backend imports the third-party client lazily, so a missing
    # package would first surface as an exception on a throttled request. Fail at
    # startup with an actionable message instead of serving 5xx on every login.
    try:
        import redis  # noqa: F401  (presence check only)
    except ImportError as exc:
        raise ImproperlyConfigured(
            "REDIS_URL is configured but the 'redis' client package is not "
            "installed, so throttled endpoints would fail closed. Install the "
            "pinned dependency from requirements.txt."
        ) from exc
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
            "KEY_PREFIX": "avestellar",
            # Options are forwarded to redis.ConnectionPool; keep the timeouts
            # short so a flapping cache cannot pin worker threads.
            "OPTIONS": {
                "socket_connect_timeout": CACHE_SOCKET_TIMEOUT_SECONDS,
                "socket_timeout": CACHE_SOCKET_TIMEOUT_SECONDS,
                "max_connections": env.int("CACHE_MAX_CONNECTIONS", default=100),
            },
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "avestellar-finance",
        }
    }

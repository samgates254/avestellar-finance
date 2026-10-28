"""Small, testable helpers for fail-closed environment validation.

These functions are deliberately free of Django settings imports so they can be
unit-tested directly and reused by ``config.settings``. They raise
``ImproperlyConfigured`` with messages that never include secret values (a
secret key, a database URL, or a password is never echoed back).

The helpers take an explicit ``strict`` flag instead of re-deriving it, so the
caller decides once whether the running configuration is a deployed one. See
``strict_mode()`` for that rule.
"""

from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured

# Recognized deployment profiles. Anything else is rejected rather than guessed.
VALID_ENVIRONMENTS = frozenset({"development", "test", "staging", "production"})
STRICT_ENVIRONMENTS = frozenset({"staging", "production"})

# Values that must never be accepted as a real staging/production secret. These
# are the committed example placeholders plus the removed legacy dev fallback.
KNOWN_PLACEHOLDER_SECRETS = frozenset(
    {
        "",
        "changeme",
        "change-me",
        "change-in-production-avestellar-finance-super-secret-key-32chars",
        "django-insecure-avestellar-finance-development-key-default-32chars",
        "replace-with-a-unique-50-character-secret",
        "your-secret-key",
    }
)

MIN_SECRET_KEY_LENGTH = 32
POSTGRES_SCHEMES = ("postgres://", "postgresql://")
# Schemes that denote a cache shared between processes. The in-process LocMem
# cache is never selected through a URL: it is the explicit no-URL fallback.
SHARED_CACHE_SCHEMES = ("redis://", "rediss://", "unix://")
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def resolve_environment(raw_django_env: str, legacy_environment: str = "") -> str:
    """Normalize DJANGO_ENV, falling back to the legacy ENVIRONMENT variable.

    Unknown values are rejected: guessing a profile is how a staging process
    ends up silently running with development defaults.
    """
    value = (raw_django_env or "").strip().lower()
    if not value:
        value = (legacy_environment or "").strip().lower()
    if not value:
        return "development"
    if value not in VALID_ENVIRONMENTS:
        raise ImproperlyConfigured(
            f"DJANGO_ENV='{value}' is not a recognized environment. Must be one "
            "of: " + ", ".join(sorted(VALID_ENVIRONMENTS)) + "."
        )
    return value


def is_strict_environment(environment: str) -> bool:
    """True only for the explicitly deployed profiles."""
    return environment in STRICT_ENVIRONMENTS


def strict_mode(environment: str, debug: bool) -> bool:
    """Decide whether the fail-closed requirements apply.

    Staging/production always qualify. ``DEBUG=False`` also qualifies even if the
    profile was left at its default, so a forgotten DJANGO_ENV cannot quietly
    downgrade a deployed process to development behaviour.
    """
    return is_strict_environment(environment) or not debug


def validate_secret_key(secret: str, strict: bool) -> str:
    """Require a non-empty secret everywhere; reject placeholders when strict.

    There is no built-in fallback: every environment (including local
    development and the test suite) must supply its own key, and staging or any
    DEBUG=False process additionally rejects known placeholders and trivially
    short values.
    """
    value = (secret or "").strip()
    if not value:
        raise ImproperlyConfigured(
            "SECRET_KEY is required. Set a unique value in the environment; no "
            "built-in development fallback is provided (see .env.example)."
        )
    if strict:
        if value in KNOWN_PLACEHOLDER_SECRETS:
            raise ImproperlyConfigured(
                "SECRET_KEY is a known placeholder/development value and is "
                "rejected in staging/production and whenever DEBUG=False."
            )
        if len(value) < MIN_SECRET_KEY_LENGTH:
            raise ImproperlyConfigured(
                "SECRET_KEY is too short for a deployed configuration "
                f"(minimum {MIN_SECRET_KEY_LENGTH} characters)."
            )
    return value


def validate_allowed_hosts(hosts: list[str], strict: bool) -> list[str]:
    """Staging/production (and any DEBUG=False run) need explicit hosts."""
    values = [str(h).strip() for h in (hosts or []) if str(h).strip()]
    if not strict:
        return values
    if not values:
        raise ImproperlyConfigured(
            "ALLOWED_HOSTS must be explicitly configured in staging/production "
            "and whenever DEBUG=False."
        )
    if "*" in values:
        raise ImproperlyConfigured(
            "ALLOWED_HOSTS must not contain a wildcard ('*') in staging/production; "
            "list the exact hostnames this deployment answers for."
        )
    return values


def validate_database_url(url: str, strict: bool) -> str:
    """Validate DATABASE_URL for the active environment without echoing it.

    An explicit PostgreSQL URL is mandatory in strict mode; SQLite and every
    other backend is refused. The URL is never included in an error message
    because it may embed credentials.
    """
    value = (url or "").strip()
    if not value:
        if strict:
            raise ImproperlyConfigured(
                "DATABASE_URL is required in staging/production; refusing to "
                "fall back to SQLite."
            )
        return ""
    if "://" not in value:
        raise ImproperlyConfigured(
            "DATABASE_URL is malformed: expected scheme://user:password@host:port/database."
        )
    if not strict:
        return value
    if not value.startswith(POSTGRES_SCHEMES):
        raise ImproperlyConfigured(
            "DATABASE_URL must point at PostgreSQL in staging/production; "
            "SQLite and other backends are refused."
        )
    try:
        parts = urlsplit(value)
        hostname = parts.hostname  # raises ValueError on an invalid IPv6/port
        parts.port
    except ValueError:
        raise ImproperlyConfigured(
            "DATABASE_URL is malformed and could not be parsed."
        ) from None
    if not hostname or not parts.path.strip("/"):
        raise ImproperlyConfigured(
            "DATABASE_URL must include both a host and a database name."
        )
    return value


def validate_effective_database_engine(engine: str, strict: bool) -> None:
    """Fail closed when the *resolved* engine is not PostgreSQL in strict mode.

    This guards against the case where DATABASE_URL exists but the effective
    DATABASES configuration still resolves to SQLite or another backend.
    """
    if strict and engine != "django.db.backends.postgresql":
        raise ImproperlyConfigured(
            "The resolved database engine is not PostgreSQL; staging/production "
            "must use PostgreSQL."
        )


def validate_origin_configuration(
    allow_all: bool,
    allowed_origins: list[str],
    csrf_trusted_origins: list[str],
    strict: bool,
    allow_permissive_override: bool = False,
) -> None:
    """Reject permissive CORS/CSRF origins when the configuration is deployed.

    A wildcard is never enabled merely because DEBUG is on. Deployed
    configurations must list exact origins unless an operator sets the
    documented ``ALLOW_PERMISSIVE_CORS`` override for a narrowly justified case.
    """
    if not strict:
        return
    if allow_all and not allow_permissive_override:
        raise ImproperlyConfigured(
            "CORS_ALLOW_ALL_ORIGINS is not permitted in staging/production. List "
            "exact origins, or set ALLOW_PERMISSIVE_CORS=True with a documented "
            "justification."
        )
    if allow_permissive_override:
        return
    origins = [str(o).strip() for o in (allowed_origins or [])]
    csrf_origins = [str(o).strip() for o in (csrf_trusted_origins or [])]
    if "*" in origins or "*" in csrf_origins:
        raise ImproperlyConfigured(
            "Wildcard origins are not permitted in staged/deployed "
            "configurations; list exact CORS/CSRF origins."
        )
    insecure = [o for o in origins + csrf_origins if o and not o.lower().startswith("https://")]
    if insecure:
        raise ImproperlyConfigured(
            "CORS/CSRF origins must use https:// in staging/production; refusing "
            f"{len(insecure)} non-HTTPS origin(s)."
        )


def validate_cache_url(
    url: str,
    strict: bool,
    require_shared: bool = False,
    allow_insecure_transport: bool = False,
) -> str:
    """Validate a shared-cache URL for throttling state, without echoing it.

    An empty value means "no shared cache": the caller falls back to the
    in-process LocMem cache, which is correct for a single dev/test process and
    explicitly *not* shared across workers. ``require_shared`` turns that
    fallback into a startup failure (for deployments that must not run with
    per-process throttling).

    A configured URL is always parsed, so a typo cannot silently degrade into an
    in-process cache. In strict mode a remote ``redis://`` URL is refused in
    favour of ``rediss://``: throttle counters would otherwise cross an
    untrusted network in plaintext.

    The URL is never included in an error message because it may embed a
    password.
    """
    value = (url or "").strip()
    if not value:
        if require_shared:
            raise ImproperlyConfigured(
                "CACHE_REQUIRE_SHARED is enabled but no cache URL is configured. "
                "Without it, authentication throttling is per-process only and "
                "must not be described as distributed rate limiting."
            )
        return ""
    if "://" not in value:
        raise ImproperlyConfigured(
            "The cache URL is malformed: expected scheme://host:port/database."
        )
    if not value.startswith(SHARED_CACHE_SCHEMES):
        raise ImproperlyConfigured(
            "The cache URL must use a shared-cache scheme (redis://, rediss://, "
            "or unix://). An in-process cache is selected by leaving it unset."
        )
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
        parts.port  # raises ValueError on an invalid port
    except ValueError:
        raise ImproperlyConfigured(
            "The cache URL is malformed and could not be parsed."
        ) from None
    if value.startswith("unix://"):
        if not parts.path.strip("/"):
            raise ImproperlyConfigured(
                "The unix:// cache URL must include a socket path."
            )
        return value
    if not hostname:
        raise ImproperlyConfigured("The cache URL must include a host.")
    if (
        strict
        and value.startswith("redis://")
        and hostname not in LOOPBACK_HOSTS
        and not allow_insecure_transport
    ):
        raise ImproperlyConfigured(
            "A remote cache URL must use rediss:// (TLS) in staging/production. "
            "Set ALLOW_INSECURE_CACHE_TRANSPORT=True only for a private network "
            "you control."
        )
    return value

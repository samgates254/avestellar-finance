"""Milestone 7.1 settings-hardening tests.

``config/settings.py`` must fail closed for deployed configurations. Because the
module raises during import, each scenario runs in an isolated subprocess with a
controlled environment; the running test session's settings are never mutated.

django-environ's ``read_env()`` does not overwrite variables already present in
``os.environ``, so every key that matters is set explicitly (an empty string is
still "present" and therefore wins over the repository's local ``.env``).
``config/env_validation.py`` is also unit-tested directly: it is deliberately
free of settings, so the rules can be proven without booting Django.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.core.exceptions import ImproperlyConfigured

from config.env_validation import (
    is_strict_environment,
    resolve_environment,
    strict_mode,
    validate_allowed_hosts,
    validate_database_url,
    validate_effective_database_engine,
    validate_origin_configuration,
    validate_secret_key,
)

BASE_DIR = Path(__file__).resolve().parent.parent

_NEUTRALIZED_KEYS = (
    "DJANGO_ENV",
    "ENVIRONMENT",
    "DEBUG",
    "SECRET_KEY",
    "ALLOWED_HOSTS",
    "DATABASE_URL",
    "CORS_ALLOWED_ORIGINS",
    "CORS_ALLOW_ALL_ORIGINS",
    "CSRF_TRUSTED_ORIGINS",
    "ALLOW_PERMISSIVE_CORS",
    "REDIS_URL",
    "CACHE_REQUIRE_SHARED",
    "ALLOW_INSECURE_CACHE_TRANSPORT",
    "CACHE_SOCKET_TIMEOUT_SECONDS",
    "CACHE_MAX_CONNECTIONS",
    "E2E_AV_DATABASE_NAME",
)

# A staging profile that is expected to import cleanly.
_VALID_STAGING = {
    "DJANGO_ENV": "staging",
    "DEBUG": "False",
    "SECRET_KEY": "s" * 50,
    "ALLOWED_HOSTS": "staging.example.com",
    "DATABASE_URL": "postgresql://staging_user:staging_pass@db.example.com:5432/avestellar",
}


def _run(env_overrides: dict[str, str], script: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    for key in _NEUTRALIZED_KEYS:
        env.pop(key, None)
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(BASE_DIR),
        env=env,
        capture_output=True,
        text=True,
    )


def _import_settings(env_overrides: dict[str, str]) -> subprocess.CompletedProcess:
    return _run(env_overrides, "import config.settings; print('IMPORT_OK')")


# ---------------------------------------------------------------------------
# Helper unit tests (no Django boot required)
# ---------------------------------------------------------------------------
class TestEnvironmentHelpers:
    def test_unknown_environment_is_rejected(self):
        with pytest.raises(ImproperlyConfigured) as excinfo:
            resolve_environment("qa")
        assert "not a recognized environment" in str(excinfo.value)

    def test_legacy_environment_alias_is_honoured(self):
        assert resolve_environment("", "staging") == "staging"
        assert resolve_environment("production", "development") == "production"

    def test_environment_defaults_to_development(self):
        assert resolve_environment("", "") == "development"

    def test_strict_mode_covers_staging_and_debug_off(self):
        assert is_strict_environment("staging") is True
        assert is_strict_environment("production") is True
        assert is_strict_environment("development") is False
        assert strict_mode("development", debug=False) is True
        assert strict_mode("development", debug=True) is False


class TestSecretValidation:
    def test_missing_secret_is_rejected_everywhere(self):
        with pytest.raises(ImproperlyConfigured):
            validate_secret_key("", strict=False)
        with pytest.raises(ImproperlyConfigured):
            validate_secret_key("   ", strict=True)

    def test_known_placeholders_rejected_when_strict(self):
        for placeholder in (
            "django-insecure-avestellar-finance-development-key-default-32chars",
            "replace-with-a-unique-50-character-secret",
            "changeme",
        ):
            with pytest.raises(ImproperlyConfigured):
                validate_secret_key(placeholder, strict=True)

    def test_short_secret_rejected_when_strict(self):
        with pytest.raises(ImproperlyConfigured):
            validate_secret_key("short-key", strict=True)
        # Development ergonomics: any non-empty value is accepted.
        assert validate_secret_key("short-key", strict=False) == "short-key"


class TestDatabaseValidation:
    def test_missing_url_allowed_only_outside_strict_mode(self):
        assert validate_database_url("", strict=False) == ""
        with pytest.raises(ImproperlyConfigured):
            validate_database_url("", strict=True)

    def test_sqlite_and_malformed_urls_rejected_when_strict(self):
        for bad in ("sqlite:///db.sqlite3", "mysql://u:p@h:3306/db"):
            with pytest.raises(ImproperlyConfigured) as excinfo:
                validate_database_url(bad, strict=True)
            assert "PostgreSQL" in str(excinfo.value)
        with pytest.raises(ImproperlyConfigured) as excinfo:
            validate_database_url("not-a-url", strict=True)
        assert "malformed" in str(excinfo.value)

    def test_postgres_url_without_host_or_database_is_rejected(self):
        for bad in ("postgresql:///db", "postgresql://user:pass@host:5432/"):
            with pytest.raises(ImproperlyConfigured):
                validate_database_url(bad, strict=True)

    def test_effective_engine_is_checked_not_the_variable(self):
        # A DATABASE_URL may be present while the resolved engine is still SQLite;
        # enforcement keys off the effective engine.
        with pytest.raises(ImproperlyConfigured):
            validate_effective_database_engine("django.db.backends.sqlite3", True)
        validate_effective_database_engine("django.db.backends.postgresql", True)
        validate_effective_database_engine("django.db.backends.sqlite3", False)

    def test_secret_never_appears_in_error_message(self):
        try:
            validate_database_url("postgresql://user:sup3rs3cret@h:5432/db", strict=False)
            validate_database_url("postgresql://user:sup3rs3cret@/db", strict=True)
        except ImproperlyConfigured as exc:
            assert "sup3rs3cret" not in str(exc)


class TestOriginValidation:
    def test_wildcards_rejected_when_strict(self):
        with pytest.raises(ImproperlyConfigured):
            validate_origin_configuration(True, [], [], strict=True)
        with pytest.raises(ImproperlyConfigured):
            validate_origin_configuration(False, ["*"], [], strict=True)
        with pytest.raises(ImproperlyConfigured):
            validate_origin_configuration(False, [], ["*"], strict=True)

    def test_explicit_origins_and_override_are_allowed(self):
        validate_origin_configuration(
            False, ["https://app.example.com"], ["https://app.example.com"], strict=True
        )
        validate_origin_configuration(True, [], [], strict=True, allow_permissive_override=True)

    def test_permissive_configuration_is_a_local_affair(self):
        # Development is allowed to be permissive; the point is that nothing turns
        # it on automatically.
        validate_origin_configuration(True, ["*"], ["*"], strict=False)

    def test_plaintext_origins_rejected_when_strict(self):
        with pytest.raises(ImproperlyConfigured) as excinfo:
            validate_origin_configuration(
                False, ["http://app.example.com"], [], strict=True
            )
        assert "https" in str(excinfo.value)
        with pytest.raises(ImproperlyConfigured):
            validate_origin_configuration(
                False, [], ["null"], strict=True
            )
        # The documented override still wins.
        validate_origin_configuration(
            False, ["http://app.example.com"], [], strict=True, allow_permissive_override=True
        )


class TestAllowedHosts:
    def test_explicit_hosts_required_when_strict(self):
        with pytest.raises(ImproperlyConfigured):
            validate_allowed_hosts([], True)
        assert validate_allowed_hosts(["api.example.com"], True) == ["api.example.com"]
        assert validate_allowed_hosts([], False) == []

    def test_wildcard_hosts_rejected_when_strict(self):
        with pytest.raises(ImproperlyConfigured) as excinfo:
            validate_allowed_hosts(["example.com", "*"], True)
        assert "wildcard" in str(excinfo.value)
        # Development may still use it deliberately.
        assert validate_allowed_hosts(["*"], False) == ["*"]


# ---------------------------------------------------------------------------
# Whole-module boot tests
# ---------------------------------------------------------------------------
def test_valid_staging_configuration_boots():
    result = _import_settings(_VALID_STAGING)
    assert result.returncode == 0, result.stderr
    assert "IMPORT_OK" in result.stdout


def test_staging_boot_uses_postgresql_and_strict_security():
    script = (
        "import config.settings as s\n"
        "assert s.DJANGO_ENV == 'staging', s.DJANGO_ENV\n"
        "assert s.STRICT_CONFIG is True\n"
        "assert s.DATABASES['default']['ENGINE'] == 'django.db.backends.postgresql'\n"
        "assert s.SESSION_COOKIE_SECURE is True\n"
        "assert s.SECURE_SSL_REDIRECT is True\n"
        "assert s.X_FRAME_OPTIONS == 'DENY'\n"
        "print('STAGING_OK')\n"
    )
    result = _run(_VALID_STAGING, script)
    assert result.returncode == 0, result.stderr
    assert "STAGING_OK" in result.stdout


@pytest.mark.parametrize(
    "secret",
    [
        "",
        "changeme",
        "replace-with-a-unique-50-character-secret",
        "django-insecure-avestellar-finance-development-key-default-32chars",
        "too-short",
    ],
)
def test_missing_or_placeholder_secret_fails_closed(secret):
    result = _import_settings(dict(_VALID_STAGING, SECRET_KEY=secret))
    assert result.returncode != 0
    assert "SECRET_KEY" in result.stderr


def test_missing_allowed_hosts_fails_closed():
    result = _import_settings(dict(_VALID_STAGING, ALLOWED_HOSTS=""))
    assert result.returncode != 0
    assert "ALLOWED_HOSTS" in result.stderr


def test_missing_database_url_fails_closed():
    result = _import_settings(dict(_VALID_STAGING, DATABASE_URL=""))
    assert result.returncode != 0
    assert "DATABASE_URL" in result.stderr


def test_malformed_database_url_fails_closed():
    result = _import_settings(dict(_VALID_STAGING, DATABASE_URL="postgres://user:pw@"))
    assert result.returncode != 0
    # The credential-bearing URL is never echoed back.
    assert "user:pw" not in result.stderr


def test_sqlite_is_refused_in_staging():
    result = _import_settings(dict(_VALID_STAGING, DATABASE_URL="sqlite:///db.sqlite3"))
    assert result.returncode != 0
    assert "PostgreSQL" in result.stderr


def test_unknown_environment_name_fails_closed():
    result = _import_settings(dict(_VALID_STAGING, DJANGO_ENV="qa"))
    assert result.returncode != 0
    assert "not a recognized environment" in result.stderr


def test_permissive_cors_fails_closed_in_staging():
    result = _import_settings(dict(_VALID_STAGING, CORS_ALLOW_ALL_ORIGINS="True"))
    assert result.returncode != 0
    assert "CORS_ALLOW_ALL_ORIGINS" in result.stderr


def test_wildcard_csrf_origin_fails_closed_in_staging():
    result = _import_settings(dict(_VALID_STAGING, CSRF_TRUSTED_ORIGINS="*"))
    assert result.returncode != 0
    assert "Wildcard origins" in result.stderr


def test_documented_permissive_override_is_honoured():
    result = _import_settings(
        dict(_VALID_STAGING, CORS_ALLOW_ALL_ORIGINS="True", ALLOW_PERMISSIVE_CORS="True")
    )
    assert result.returncode == 0, result.stderr


def test_debug_defaults_to_false():
    # The module's declared default for DEBUG is False. The repository's local
    # .env supplies DEBUG=True for developer ergonomics, so the default itself is
    # asserted here, and ``strict_mode(..., debug=False)`` is covered above.
    import config.settings as settings_module

    assert settings_module.environ_env_defaults["DEBUG"] == (bool, False)


def test_local_development_boots_with_sqlite_and_no_wildcard_cors():
    env = {
        "DJANGO_ENV": "development",
        "DEBUG": "True",
        "SECRET_KEY": "local-development-key-not-for-deployment",
        "ALLOWED_HOSTS": "",
        "DATABASE_URL": "",
    }
    script = (
        "import config.settings as s\n"
        "assert s.DEBUG is True\n"
        "assert s.STRICT_CONFIG is False\n"
        "assert s.DATABASES['default']['ENGINE'] == 'django.db.backends.sqlite3'\n"
        "assert s.CORS_ALLOW_ALL_ORIGINS is False, 'wildcard CORS must not follow DEBUG'\n"
        "assert s.CORS_ALLOWED_ORIGINS == []\n"
        "assert 'localhost' in s.ALLOWED_HOSTS\n"
        "assert s.MAX_STATEMENT_UPLOAD_BYTES == 15 * 1024 * 1024\n"
        "assert s.FILE_UPLOAD_MAX_MEMORY_SIZE < s.MAX_STATEMENT_UPLOAD_BYTES\n"
        "print('DEV_OK')\n"
    )
    result = _run(env, script)
    assert result.returncode == 0, result.stderr
    assert "DEV_OK" in result.stdout


def test_guarded_e2e_settings_refuse_a_non_disposable_database():
    env = dict(_VALID_STAGING, E2E_AV_DATABASE_NAME="avestellar_finance")
    result = _run(env, "import config.live_http_e2e_settings; print('E2E_OK')")
    assert result.returncode != 0
    assert "avestellar_live_http_" in result.stderr


def test_guarded_e2e_settings_require_postgresql():
    env = {
        "DJANGO_ENV": "test",
        "DEBUG": "True",
        "SECRET_KEY": "e2e-settings-import-check-key",
        "ALLOWED_HOSTS": "localhost",
        "DATABASE_URL": "",
        "E2E_AV_DATABASE_NAME": "avestellar_live_http_unit",
    }
    result = _run(env, "import config.live_http_e2e_settings; print('E2E_OK')")
    assert result.returncode != 0
    assert "PostgreSQL" in result.stderr

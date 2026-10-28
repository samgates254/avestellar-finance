"""Shared-cache configuration for authentication throttling.

Milestone 7.1 documented the honest limitation: without a shared cache the
throttle counters live in each process, so a multi-worker deployment has no
effective rate limit. These tests cover the configuration layer added on top of
that:

* a cache URL is validated (scheme/host/port) instead of being trusted blindly;
* the in-process fallback remains the explicit, local-only default;
* deployed profiles refuse a plaintext remote cache and can require sharing;
* the URL (which may embed a password) never appears in an error message.

No Redis server is contacted: these are configuration assertions, matching the
rule that distributed throttling must not be reported as active until a shared
cache has actually been configured *and verified* (see
``scripts/check_shared_cache.py``).
"""

from pathlib import Path

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from config.env_validation import validate_cache_url
from tests.test_settings_hardening import _VALID_STAGING, _run

REDIS_BACKEND = "django.core.cache.backends.redis.RedisCache"
LOCMEM_BACKEND = "django.core.cache.backends.locmem.LocMemCache"


class TestCacheUrlValidation:
    def test_empty_is_allowed_for_local_development(self):
        assert validate_cache_url("", strict=False) == ""
        assert validate_cache_url("   ", strict=True) == ""

    def test_empty_fails_closed_when_sharing_is_required(self):
        with pytest.raises(ImproperlyConfigured) as excinfo:
            validate_cache_url("", strict=True, require_shared=True)
        assert "CACHE_REQUIRE_SHARED" in str(excinfo.value)
        assert "per-process" in str(excinfo.value)

    def test_shared_schemes_are_accepted(self):
        for url in (
            "redis://localhost:6379/0",
            "rediss://cache.internal:6380/1",
            "unix:///run/redis/avestellar.sock",
        ):
            assert validate_cache_url(url, strict=True) == url

    def test_in_process_or_unsupported_schemes_are_rejected(self):
        for url in ("locmem://default", "memcached://cache:11211", "file:///tmp/cache"):
            with pytest.raises(ImproperlyConfigured) as excinfo:
                validate_cache_url(url, strict=True)
            assert "shared-cache scheme" in str(excinfo.value)

    def test_malformed_urls_are_rejected(self):
        for url in ("redis://", "redis:///0", "redis://cache:not-a-port/0", "not-a-url"):
            with pytest.raises(ImproperlyConfigured):
                validate_cache_url(url, strict=True)

    def test_unix_socket_requires_a_path(self):
        with pytest.raises(ImproperlyConfigured):
            validate_cache_url("unix://", strict=False)

    def test_remote_plaintext_cache_is_rejected_when_strict(self):
        with pytest.raises(ImproperlyConfigured) as excinfo:
            validate_cache_url("redis://cache.internal:6379/0", strict=True)
        assert "rediss://" in str(excinfo.value)
        # Documented override for a private network.
        url = "redis://cache.internal:6379/0"
        assert validate_cache_url(url, strict=True, allow_insecure_transport=True) == url

    def test_loopback_plaintext_cache_is_accepted_when_strict(self):
        for url in ("redis://127.0.0.1:6379/0", "redis://localhost:6379/0"):
            assert validate_cache_url(url, strict=True) == url

    def test_cache_password_is_never_echoed(self):
        secret = "sup3rsecret-cache-password"
        for url in (
            f"redis://default:{secret}@cache.internal:6379/0",
            f"redis://default:{secret}@cache.internal:not-a-port/0",
        ):
            assert secret in url  # guard against a vacuous assertion
            with pytest.raises(ImproperlyConfigured) as excinfo:
                validate_cache_url(url, strict=True)
            assert secret not in str(excinfo.value)


class TestCacheSettings:
    def test_reported_state_matches_the_configured_backend(self):
        configured = bool(settings.REDIS_URL)
        assert settings.SHARED_CACHE_CONFIGURED is configured
        backend = settings.CACHES["default"]["BACKEND"]
        if configured:
            assert backend == REDIS_BACKEND
            assert settings.SHARED_CACHE_USES_TLS is settings.REDIS_URL.startswith(
                "rediss://"
            )
            assert settings.CACHES["default"]["KEY_PREFIX"] == "avestellar"
            assert settings.CACHES["default"]["OPTIONS"]["socket_connect_timeout"] > 0
        else:
            assert backend == LOCMEM_BACKEND
            assert settings.SHARED_CACHE_USES_TLS is False

    def test_local_profile_does_not_claim_distributed_throttling(self):
        # The committed example configuration ships without a cache URL, so the
        # development profile must report "not shared" rather than pretend.
        assert settings.SHARED_CACHE_CONFIGURED is False


def _cache_script(*assertions: str) -> str:
    return "import config.settings as s\n" + "\n".join(assertions) + "\nprint('CACHE_OK')"


def test_staging_with_tls_cache_configures_the_shared_backend():
    script = _cache_script(
        "assert s.SHARED_CACHE_CONFIGURED is True",
        "assert s.SHARED_CACHE_USES_TLS is True",
        f"assert s.CACHES['default']['BACKEND'] == '{REDIS_BACKEND}'",
        "assert s.CACHES['default']['LOCATION'].startswith('rediss://')",
    )
    result = _run(
        dict(_VALID_STAGING, REDIS_URL="rediss://cache.internal:6380/1"), script
    )
    assert result.returncode == 0, result.stderr
    assert "CACHE_OK" in result.stdout


def test_staging_rejects_a_plaintext_remote_cache_without_leaking_it():
    result = _run(
        dict(_VALID_STAGING, REDIS_URL="redis://:topsecret@cache.internal:6379/0"),
        "import config.settings",
    )
    assert result.returncode != 0
    assert "rediss" in result.stderr
    assert "topsecret" not in result.stderr
    assert "cache.internal" not in result.stderr


def test_requiring_a_shared_cache_without_one_fails_closed():
    result = _run(
        dict(_VALID_STAGING, CACHE_REQUIRE_SHARED="True"),
        "import config.settings",
    )
    assert result.returncode != 0
    assert "CACHE_REQUIRE_SHARED" in result.stderr


def test_development_keeps_the_in_process_cache():
    script = _cache_script(
        "assert s.SHARED_CACHE_CONFIGURED is False",
        f"assert s.CACHES['default']['BACKEND'] == '{LOCMEM_BACKEND}'",
    )
    result = _run({"DEBUG": "True", "DJANGO_ENV": "development"}, script)
    assert result.returncode == 0, result.stderr
    assert "CACHE_OK" in result.stdout


def test_redis_client_is_a_pinned_dependency():
    """Configuring REDIS_URL is useless if the client cannot be installed."""
    requirements = (Path(__file__).resolve().parent.parent / "requirements.txt").read_text()
    pinned = [
        line.strip()
        for line in requirements.splitlines()
        if line.strip().lower().startswith("redis")
    ]
    assert pinned and all("==" in line for line in pinned), (
        "requirements.txt must pin the redis client, otherwise a configured "
        "shared cache cannot be reached in staging."
    )


def test_missing_redis_client_fails_at_startup():
    """A lazy backend import would surface as 5xx on login; refuse to boot.

    ``sys.modules['redis'] = None`` makes ``import redis`` raise without touching
    the installed package.
    """
    result = _run(
        dict(_VALID_STAGING, REDIS_URL="rediss://cache.internal:6380/1"),
        "import sys\n"
        "sys.modules['redis'] = None\n"
        "import config.settings\n"
        "print('CACHE_OK')",
    )
    assert result.returncode != 0
    assert "CACHE_OK" not in result.stdout
    assert "'redis' client package" in result.stderr
    assert "cache.internal" not in result.stderr

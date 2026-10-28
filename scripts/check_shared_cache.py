"""Verify the configured cache is a working, shared cache.

Authentication throttling keeps its counters in the Django cache. A cache URL
being present proves only that someone typed it: this script performs a
round-trip and reports whether throttling state is actually shared, so a
deployment never claims "distributed rate limiting" without evidence.

Usage (from the repository root, with the target environment loaded):

    python scripts/check_shared_cache.py
    python scripts/check_shared_cache.py --require-shared   # exit 1 if per-process

Exit codes: 0 = shared cache verified (or not required and absent),
1 = required-but-absent, or the round-trip failed.

Safety: the cache URL is never printed because it may embed a password. Errors
are reported by exception class only.
"""

import argparse
import os
import sys
from pathlib import Path

import django

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.conf import settings  # noqa: E402
from django.core.cache import cache  # noqa: E402

PROBE_KEY = "avestellar:shared-cache-probe"
PROBE_VALUE = "ok"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--require-shared",
        action="store_true",
        help="Fail when no shared cache is configured (multi-worker deployments).",
    )
    args = parser.parse_args()

    backend = settings.CACHES["default"]["BACKEND"]
    shared = bool(getattr(settings, "SHARED_CACHE_CONFIGURED", False))
    uses_tls = bool(getattr(settings, "SHARED_CACHE_USES_TLS", False))

    print(f"cache backend: {backend}")
    print(f"shared cache configured: {shared}")
    if shared:
        print(f"cache transport TLS: {uses_tls}")
        if not uses_tls:
            print(
                "WARNING: the cache URL is not TLS (rediss://). Acceptable only on "
                "a trusted private network; throttle counters cross it in plaintext."
            )

    required = args.require_shared or os.environ.get("CACHE_REQUIRE_SHARED", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    if not shared:
        print(
            "RESULT: no shared cache. Throttling is per-process only and must NOT be "
            "described as distributed rate limiting."
        )
        return 1 if required else 0

    try:
        cache.set(PROBE_KEY, PROBE_VALUE, timeout=10)
        observed = cache.get(PROBE_KEY)
        cache.delete(PROBE_KEY)
    except Exception as exc:  # noqa: BLE001 - report the class, never the URL
        print(f"RESULT: cache round-trip FAILED ({type(exc).__name__}).")
        print(
            "Throttled endpoints fail closed while the cache is unreachable; fix "
            "connectivity before reporting distributed throttling."
        )
        return 1

    if observed != PROBE_VALUE:
        print("RESULT: cache round-trip returned an unexpected value.")
        return 1

    print("RESULT: shared cache verified (round-trip OK). Distributed throttling state is available.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

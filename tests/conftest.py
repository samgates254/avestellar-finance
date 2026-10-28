import pytest
from django.core.cache import cache


@pytest.fixture(autouse=True)
def _reset_cache_between_tests():
    """Clear the throttle cache so ScopedRateThrottle counters do not leak.

    Login/registration throttling uses the Django cache. Under LocMemCache the
    counters persist for the whole pytest process, so without this reset a
    later test could observe a spurious HTTP 429 purely because an earlier test
    exercised the same endpoint. Each test starts from a clean cache.
    """
    cache.clear()
    yield
    cache.clear()

"""Settings overlay restricted to a dedicated disposable local PostgreSQL DB."""

import os

from django.core.exceptions import ImproperlyConfigured

from config.settings import *  # noqa: F403

test_database = os.environ.get("E2E_AV_DATABASE_NAME", "")
if DATABASES["default"]["ENGINE"] != "django.db.backends.postgresql":  # noqa: F405
    raise ImproperlyConfigured("The live HTTP fixture requires PostgreSQL.")
if not test_database.startswith("avestellar_live_http_"):
    raise ImproperlyConfigured(
        "E2E_AV_DATABASE_NAME must use the avestellar_live_http_* disposable prefix."
    )

DATABASES["default"]["NAME"] = test_database  # noqa: F405
DATABASES["default"]["CONN_MAX_AGE"] = 0  # noqa: F405
DEBUG = True
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
SECRET_KEY = "synthetic-local-http-integration-only"

# E2E drives the real login endpoint repeatedly across sequential journeys.
# Rate limiting is validated by the dedicated backend security suite, so it is
# relaxed here to avoid spurious HTTP 429s interrupting full-stack journeys.
REST_FRAMEWORK = {  # noqa: F405
    **REST_FRAMEWORK,  # noqa: F405
    "DEFAULT_THROTTLE_RATES": {
        **REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"],  # noqa: F405
        "login": "1000/hour",
        "register": "1000/hour",
    },
}

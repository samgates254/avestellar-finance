"""Create one empty, explicitly named PostgreSQL database for local E2E use."""

import os
import re
import sys
from pathlib import Path

import django

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.core.exceptions import ImproperlyConfigured  # noqa: E402
from django.db import connection  # noqa: E402


name = os.environ.get("E2E_AV_DATABASE_NAME", "")
if not re.fullmatch(r"avestellar_live_http_[a-zA-Z0-9_]+", name):
    raise ImproperlyConfigured(
        "E2E_AV_DATABASE_NAME must be a new avestellar_live_http_* database name."
    )
if connection.vendor != "postgresql":
    raise ImproperlyConfigured("A configured PostgreSQL connection is required to create the isolated DB.")

connection.ensure_connection()
with connection.cursor() as cursor:
    cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", [name])
    if cursor.fetchone():
        raise ImproperlyConfigured(f"Database {name} already exists; refusing to modify it.")
    cursor.execute(f"CREATE DATABASE {connection.ops.quote_name(name)}")
print(f"Created empty disposable PostgreSQL database {name}.")

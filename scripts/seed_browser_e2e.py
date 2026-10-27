"""Seed synthetic tenants for the isolated browser E2E run.

Safety contract (mirrors create_live_http_database.py / live_http_e2e_settings.py):
this refuses to run unless the active connection is PostgreSQL AND the database
name carries the disposable ``avestellar_live_http_`` prefix, so it can never
touch the normal ``avestellar_finance`` development database that holds real
business records.

Everything created here is synthetic. No real customer, payment provider,
MauzoPilot, or Safaricom resource is contacted.
"""

import os
import sys
from pathlib import Path

import django

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.core.exceptions import ImproperlyConfigured  # noqa: E402
from django.db import connection  # noqa: E402

from identity.models import User  # noqa: E402
from invoicing.models import Customer  # noqa: E402
from ledger.services.initialization import initialize_organization_accounts  # noqa: E402
from organizations.models import Membership, Organization, RoleChoices  # noqa: E402

PASSWORD = "synthetic-browser-e2e"

if connection.vendor != "postgresql":
    raise ImproperlyConfigured("Refusing to seed: PostgreSQL is required for the browser E2E fixture.")
if not str(connection.settings_dict["NAME"]).startswith("avestellar_live_http_"):
    raise ImproperlyConfigured(
        "Refusing to seed outside the isolated avestellar_live_http_* database."
    )


def make_org(name: str, slug: str) -> Organization:
    org, _ = Organization.objects.get_or_create(
        slug=slug, defaults={"name": name, "currency": "KES", "currency_symbol": "KSh", "decimal_places": 2}
    )
    initialize_organization_accounts(org)
    return org


def make_user(email: str) -> User:
    user, _ = User.objects.get_or_create(
        email=email, defaults={"first_name": "Synthetic", "last_name": "E2E"}
    )
    user.set_password(PASSWORD)
    user.save()
    return user


def grant(user: User, org: Organization, role: str) -> None:
    Membership.objects.update_or_create(
        organization=org, user=user, defaults={"role": role, "is_active": True}
    )


alpha = make_org("Browser Tenant Alpha", "browser-e2e-alpha")
beta = make_org("Browser Tenant Beta", "browser-e2e-beta")

accountant = make_user("accountant@browser-e2e.invalid")
viewer = make_user("viewer@browser-e2e.invalid")

# The accountant owns both tenants: exercises organization selection, scoped
# data, journal/invoice/statement/reconciliation flows, and admin reopen rights.
grant(accountant, alpha, RoleChoices.OWNER)
grant(accountant, beta, RoleChoices.OWNER)
# The viewer belongs to Alpha only: exercises restricted-mutation and
# cross-organization isolation (they are not a member of Beta at all).
grant(viewer, alpha, RoleChoices.VIEWER)

Customer.objects.get_or_create(
    organization=alpha, name="Synthetic Acme Ltd",
    defaults={"email": "acme@example.invalid", "currency": "KES", "is_active": True},
)

print(
    "Seeded browser E2E fixture: "
    f"alpha={alpha.id} beta={beta.id} "
    f"accountant={accountant.email} viewer={viewer.email} db={connection.settings_dict['NAME']}"
)

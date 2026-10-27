"""Seed a synthetic AVESTELLAR tenant for the local HTTP integration runner."""

import os
from pathlib import Path

import django

django.setup()

from django.core.exceptions import ImproperlyConfigured  # noqa: E402
from django.db import connection  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from rest_framework_simplejwt.tokens import RefreshToken  # noqa: E402
from integrations.models import MauzoPilotConfig  # noqa: E402
from ledger.services.initialization import initialize_organization_accounts  # noqa: E402
from organizations.models import Membership, Organization, RoleChoices  # noqa: E402


state_dir = Path(os.environ.get("E2E_STATE_DIR", ""))
business_file = state_dir / "mauzo_business_id"
token_file = state_dir / "mauzo_token"
secret = os.environ.get("E2E_WEBHOOK_SECRET", "")
if connection.vendor != "postgresql" or not str(connection.settings_dict["NAME"]).startswith(
    "avestellar_live_http_"
):
    raise ImproperlyConfigured("Refusing to seed outside the isolated AVESTELLAR PostgreSQL fixture.")
if not state_dir.is_dir() or not business_file.exists() or not token_file.exists() or not secret:
    raise ImproperlyConfigured("Seed MauzoPilot first and provide E2E_STATE_DIR and E2E_WEBHOOK_SECRET.")
if Organization.objects.filter(slug="synthetic-e2e-tenant").exists():
    raise ImproperlyConfigured("The AVESTELLAR fixture database already contains the synthetic tenant.")

user = get_user_model().objects.create_user(
    email="av-e2e-owner@example.invalid",
    password="synthetic-test-only",
    first_name="Synthetic",
    last_name="Owner",
)
org = Organization.objects.create(
    name="Synthetic AVESTELLAR Tenant",
    slug="synthetic-e2e-tenant",
    currency="KES",
)
Membership.objects.create(organization=org, user=user, role=RoleChoices.OWNER, is_active=True)
initialize_organization_accounts(org, user=user)
MauzoPilotConfig.objects.create(
    organization=org,
    base_url="http://127.0.0.1:8101",
    business_id=business_file.read_text().strip(),
    api_key=token_file.read_text().strip(),
    webhook_secret=secret,
    is_active=True,
    sync_customers=True,
    sync_sales=True,
    sync_payments=True,
    request_timeout=5,
)
token = str(RefreshToken.for_user(user).access_token)
target = state_dir / "av_access_token"
target.write_text(token)
target.chmod(0o600)
target = state_dir / "av_organization_id"
target.write_text(str(org.id))
target.chmod(0o600)
print(f"Created synthetic AVESTELLAR organization {org.id}; access token saved under {state_dir}.")

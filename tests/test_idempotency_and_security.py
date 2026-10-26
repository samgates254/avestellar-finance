from decimal import Decimal
import pytest
from rest_framework.test import APIClient
from identity.models import User
from invoicing.models import Customer, Invoice
from ledger.services.initialization import initialize_organization_accounts
from organizations.models import Membership, Organization, RoleChoices


@pytest.mark.django_db
class TestIdempotencyAndSecurity:
    def setup_method(self):
        self.client = APIClient()
        self.user = User.objects.create_user(email="security@avestellar.com", password="Password123!")
        self.org = Organization.objects.create(name="Security Org", slug="sec-org", currency="KES")
        Membership.objects.create(organization=self.org, user=self.user, role=RoleChoices.ACCOUNTANT)
        initialize_organization_accounts(self.org, self.user)
        self.client.force_authenticate(user=self.user)

    def test_customer_creation_idempotency(self):
        idempotency_key = "idemp-cust-key-9988"
        payload = {
            "name": "Nairobi Solar Tech",
            "email": "info@nairobisolar.ke",
            "phone": "+254700998877",
        }

        # First request
        r1 = self.client.post(
            "/api/v1/invoicing/customers/",
            payload,
            format="json",
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
            HTTP_IDEMPOTENCY_KEY=idempotency_key,
        )
        assert r1.status_code == 201
        customer_id = r1.data["id"]

        # Duplicate request with same Idempotency-Key and same payload
        r2 = self.client.post(
            "/api/v1/invoicing/customers/",
            payload,
            format="json",
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
            HTTP_IDEMPOTENCY_KEY=idempotency_key,
        )
        assert r2.status_code == 201

        # Assert no duplicate customer was created in the database
        assert Customer.objects.filter(organization=self.org, name="Nairobi Solar Tech").count() == 1

    def test_tampered_request_with_same_idempotency_key_fails_or_creates_conflict(self):
        idempotency_key = "idemp-tamper-key-1122"
        payload1 = {"name": "First Name"}
        r1 = self.client.post(
            "/api/v1/invoicing/customers/",
            payload1,
            format="json",
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
            HTTP_IDEMPOTENCY_KEY=idempotency_key,
        )
        assert r1.status_code == 201

        # Second request with different payload under same idempotency key
        payload2 = {"name": "Completely Different Name"}
        r2 = self.client.post(
            "/api/v1/invoicing/customers/",
            payload2,
            format="json",
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
            HTTP_IDEMPOTENCY_KEY=idempotency_key,
        )
        assert r2.status_code in (409, 201)
        if r2.status_code == 409:
            assert "detail" in r2.data

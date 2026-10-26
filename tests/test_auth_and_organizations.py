import pytest
from rest_framework.test import APIClient
from identity.models import User
from organizations.models import Membership, Organization, RoleChoices


@pytest.mark.django_db
class TestAuthAndTenantIsolation:
    def setup_method(self):
        self.client = APIClient()
        self.user1 = User.objects.create_user(
            email="owner@avestellar.com", password="SecurePassword123!", first_name="Avestellar", last_name="Owner"
        )
        self.user2 = User.objects.create_user(
            email="outsider@external.com", password="SecurePassword123!", first_name="External", last_name="User"
        )

    def test_user_registration_and_jwt_token(self):
        resp = self.client.post("/api/v1/auth/register/", {
            "email": "newuser@avestellar.com",
            "password": "StrongPassword999!",
            "first_name": "New",
            "last_name": "User",
        })
        assert resp.status_code == 201
        assert resp.data["email"] == "newuser@avestellar.com"

        # Login to obtain JWT
        token_resp = self.client.post("/api/v1/auth/token/", {
            "email": "newuser@avestellar.com",
            "password": "StrongPassword999!",
        })
        assert token_resp.status_code == 200
        assert "access" in token_resp.data
        assert "refresh" in token_resp.data

    def test_organization_creation_sets_owner_and_default_accounts(self):
        self.client.force_authenticate(user=self.user1)
        resp = self.client.post("/api/v1/organizations/", {
            "name": "Avestellar Logistics Ltd",
            "slug": "avestellar-logistics",
            "currency": "KES",
        })
        assert resp.status_code == 201
        org_id = resp.data["id"]

        # Check membership is OWNER
        membership = Membership.objects.filter(organization_id=org_id, user=self.user1).first()
        assert membership is not None
        assert membership.role == RoleChoices.OWNER

        # Check standard Chart of Accounts was initialized
        org = Organization.objects.get(id=org_id)
        assert org.accounts.count() >= 10
        assert org.accounts.filter(code="1000").exists()
        assert org.accounts.filter(code="1200").exists()

    def test_tenant_isolation_user_cannot_access_unauthorized_organization_data(self):
        # Create Org 1 for User 1
        org1 = Organization.objects.create(name="Tenant One", slug="tenant-one", currency="KES")
        Membership.objects.create(organization=org1, user=self.user1, role=RoleChoices.OWNER)

        # Create Org 2 for User 2
        org2 = Organization.objects.create(name="Tenant Two", slug="tenant-two", currency="KES")
        Membership.objects.create(organization=org2, user=self.user2, role=RoleChoices.OWNER)

        # User 2 tries to access Org 1's accounts
        self.client.force_authenticate(user=self.user2)
        resp = self.client.get(
            "/api/v1/ledger/accounts/",
            HTTP_X_ORGANIZATION_ID=str(org1.id),
        )
        # Middleware will not set request.organization because user2 is not a member of org1
        # Thus the view returns empty or forbidden
        assert resp.status_code in (200, 403)
        if resp.status_code == 200:
            assert len(resp.data.get("results", resp.data)) == 0

        # User 2 tries to retrieve Org 1 detail directly
        detail_resp = self.client.get(
            f"/api/v1/organizations/{org1.id}/",
            HTTP_X_ORGANIZATION_ID=str(org1.id),
        )
        assert detail_resp.status_code in (403, 404)

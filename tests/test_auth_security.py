"""Regression tests for Milestone 7.1 authentication hardening:
login/registration throttling, refresh-token rotation with blacklisting,
revoked-token rejection, the logout/revocation endpoint, and auth errors.
"""

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from identity.models import User
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.token_blacklist.models import (
    BlacklistedToken,
    OutstandingToken,
)


@pytest.fixture(autouse=True)
def _clear_throttle_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.mark.django_db
class TestAuthThrottling:
    def setup_method(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="throttle@avestellar.com", password="StrongPassword123!"
        )

    def test_login_endpoint_throttles_after_limit(self):
        # Default login scope is 20/min; the 21st attempt is throttled regardless
        # of credential correctness.
        last = None
        for _ in range(21):
            last = self.client.post(
                "/api/v1/auth/token/",
                {"email": "throttle@avestellar.com", "password": "wrong"},
                format="json",
            )
        assert last.status_code == 429

    def test_register_endpoint_throttles_after_limit(self):
        last = None
        for i in range(11):  # register scope default is 10/hour
            last = self.client.post(
                "/api/v1/auth/register/",
                {
                    "email": f"spam{i}@avestellar.com",
                    "password": "StrongPassword123!",
                    "first_name": "S",
                    "last_name": "P",
                },
                format="json",
            )
        assert last.status_code == 429


@pytest.mark.django_db
class TestRefreshRotationAndLogout:
    def setup_method(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="rotate@avestellar.com", password="StrongPassword123!"
        )
        tokens = self.client.post(
            "/api/v1/auth/token/",
            {"email": "rotate@avestellar.com", "password": "StrongPassword123!"},
            format="json",
        )
        self.access = tokens.data["access"]
        self.refresh = tokens.data["refresh"]

    def test_refresh_rotates_and_blacklists_the_old_token(self):
        resp = self.client.post(
            "/api/v1/auth/token/refresh/", {"refresh": self.refresh}, format="json"
        )
        assert resp.status_code == 200
        assert "access" in resp.data
        # A rotated refresh token must be returned and the old one is now
        # blacklisted, so replaying it fails.
        assert "refresh" in resp.data
        replay = self.client.post(
            "/api/v1/auth/token/refresh/", {"refresh": self.refresh}, format="json"
        )
        assert replay.status_code == 401

    def test_logout_blacklists_supplied_refresh_token(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.access}")
        logout = self.client.post(
            "/api/v1/auth/logout/", {"refresh": self.refresh}, format="json"
        )
        assert logout.status_code == 205

        # The blacklisted refresh can no longer mint access tokens.
        refresh_attempt = self.client.post(
            "/api/v1/auth/token/refresh/", {"refresh": self.refresh}, format="json"
        )
        assert refresh_attempt.status_code == 401

    def test_logout_requires_authentication(self):
        anon = APIClient()
        resp = anon.post(
            "/api/v1/auth/logout/", {"refresh": self.refresh}, format="json"
        )
        assert resp.status_code == 401

    def test_logout_rejects_missing_refresh_token(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.access}")
        resp = self.client.post("/api/v1/auth/logout/", {}, format="json")
        assert resp.status_code == 400

    def test_logout_is_idempotent_for_invalid_tokens(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.access}")
        resp = self.client.post(
            "/api/v1/auth/logout/", {"refresh": "not-a-real-token"}, format="json"
        )
        # Nothing left to revoke; the client must still be able to drop its session.
        assert resp.status_code == 205
        assert "not-a-real-token" not in resp.content.decode()

    def test_repeated_logout_is_safe_and_predictable(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.access}")
        first = self.client.post(
            "/api/v1/auth/logout/", {"refresh": self.refresh}, format="json"
        )
        second = self.client.post(
            "/api/v1/auth/logout/", {"refresh": self.refresh}, format="json"
        )
        assert first.status_code == 205
        assert second.status_code == 205
        assert second.data == first.data

    def test_logout_refuses_another_users_refresh_token(self):
        User.objects.create_user(
            email="other@avestellar.com", password="StrongPassword123!"
        )
        other_tokens = self.client.post(
            "/api/v1/auth/token/",
            {"email": "other@avestellar.com", "password": "StrongPassword123!"},
            format="json",
        )
        other_refresh = other_tokens.data["refresh"]

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.access}")
        resp = self.client.post(
            "/api/v1/auth/logout/", {"refresh": other_refresh}, format="json"
        )
        assert resp.status_code == 403
        assert other_refresh not in resp.content.decode()

        # The other user's token is untouched and still rotates normally.
        still_valid = self.client.post(
            "/api/v1/auth/token/refresh/", {"refresh": other_refresh}, format="json"
        )
        assert still_valid.status_code == 200


@pytest.mark.django_db
class TestAuthErrors:
    def test_invalid_credentials_are_rejected(self):
        client = APIClient()
        User.objects.create_user(email="u@avestellar.com", password="StrongPassword123!")
        resp = client.post(
            "/api/v1/auth/token/",
            {"email": "u@avestellar.com", "password": "bad"},
            format="json",
        )
        assert resp.status_code == 401

    def test_blacklisted_token_record_created_on_rotation(self):
        # Sanity check that the token_blacklist tables are wired and used.
        client = APIClient()
        User.objects.create_user(email="bl@avestellar.com", password="StrongPassword123!")
        tokens = client.post(
            "/api/v1/auth/token/",
            {"email": "bl@avestellar.com", "password": "StrongPassword123!"},
            format="json",
        )
        refresh = tokens.data["refresh"]
        jti = RefreshToken(refresh).payload["jti"]
        # Rotation persists the outstanding token and blacklists the old one.
        client.post("/api/v1/auth/token/refresh/", {"refresh": refresh}, format="json")
        assert OutstandingToken.objects.filter(jti=jti).exists()
        # BlacklistedToken.token is the FK to OutstandingToken.
        assert BlacklistedToken.objects.filter(
            token__jti=jti
        ).exists()

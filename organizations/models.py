import uuid
from django.conf import settings
from django.db import models


class RoleChoices(models.TextChoices):
    OWNER = "OWNER", "Owner"
    ADMIN = "ADMIN", "Administrator"
    ACCOUNTANT = "ACCOUNTANT", "Accountant"
    AUDITOR = "AUDITOR", "Auditor (Read-Only)"
    VIEWER = "VIEWER", "Viewer (Read-Only)"


class Organization(models.Model):
    """Tenant model representing a company or business entity."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True, db_index=True)
    currency = models.CharField(max_length=3, default="KES")
    currency_symbol = models.CharField(max_length=10, default="KSh")
    decimal_places = models.PositiveSmallIntegerField(default=2)
    fiscal_year_start_month = models.PositiveSmallIntegerField(default=1)  # 1 = January
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "organizations"
        ordering = ["name"]

    @property
    def base_currency(self) -> str:
        return self.currency

    def __str__(self) -> str:
        return f"{self.name} ({self.currency})"


class Membership(models.Model):
    """Organization membership representing user roles within a specific tenant."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="memberships"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="organization_memberships",
    )
    role = models.CharField(
        max_length=20,
        choices=RoleChoices.choices,
        default=RoleChoices.ACCOUNTANT,
        db_index=True,
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "organization_memberships"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "user"], name="unique_org_user_membership"
            )
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.user} - {self.organization.name} [{self.role}]"

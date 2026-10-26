import uuid
from typing import Sequence
from rest_framework import permissions
from organizations.models import Membership, Organization, RoleChoices


def get_request_organization(request, view=None) -> Organization | None:
    """Helper to safely extract the active organization for the current request.

    Checks:
    1. request.organization resolved by middleware
    2. URL kwargs (e.g. 'org_id' or 'organization_id') after verifying active membership
    3. Query parameter 'org_id' after verifying active membership
    """
    if getattr(request, "organization", None):
        return request.organization

    if not request.user or not request.user.is_authenticated:
        return None

    # Check headers (supporting DRF test client force_authenticate)
    header_org_id = request.headers.get("X-Organization-ID") or request.META.get("HTTP_X_ORGANIZATION_ID")
    header_slug = request.headers.get("X-Organization-Slug") or request.META.get("HTTP_X_ORGANIZATION_SLUG")

    # Check URL kwargs
    org_id = header_org_id
    if not org_id and view and hasattr(view, "kwargs"):
        org_id = view.kwargs.get("org_id") or view.kwargs.get("organization_id")
    if not org_id:
        org_id = request.query_params.get("org_id") or request.query_params.get("organization_id")

    if org_id:
        try:
            uuid_val = uuid.UUID(str(org_id))
            membership = Membership.objects.select_related("organization").filter(
                organization__id=uuid_val,
                organization__is_active=True,
                user=request.user,
                is_active=True,
            ).first()
            if membership:
                request.membership = membership
                request.organization = membership.organization
                return membership.organization
        except (ValueError, TypeError):
            return None
    elif header_slug:
        membership = Membership.objects.select_related("organization").filter(
            organization__slug=header_slug,
            organization__is_active=True,
            user=request.user,
            is_active=True,
        ).first()
        if membership:
            request.membership = membership
            request.organization = membership.organization
            return membership.organization

    return None


class IsOrganizationMember(permissions.BasePermission):
    """Requires the user to be authenticated and an active member of the request organization."""

    def has_permission(self, request, view) -> bool:
        org = get_request_organization(request, view)
        return org is not None


class RolePermission(permissions.BasePermission):
    """Base class for role-restricted access within an organization."""

    allowed_roles: Sequence[str] = ()

    def has_permission(self, request, view) -> bool:
        org = get_request_organization(request, view)
        if not org or not getattr(request, "membership", None):
            return False
        return request.membership.role in self.allowed_roles


class IsOrgOwner(RolePermission):
    allowed_roles = (RoleChoices.OWNER,)


class IsOrgAdminOrOwner(RolePermission):
    allowed_roles = (RoleChoices.OWNER, RoleChoices.ADMIN)


class IsAccountantOrAbove(RolePermission):
    allowed_roles = (RoleChoices.OWNER, RoleChoices.ADMIN, RoleChoices.ACCOUNTANT)


class IsAuditorOrViewer(RolePermission):
    allowed_roles = (
        RoleChoices.OWNER,
        RoleChoices.ADMIN,
        RoleChoices.ACCOUNTANT,
        RoleChoices.AUDITOR,
        RoleChoices.VIEWER,
    )

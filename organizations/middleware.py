import uuid
from typing import Callable
from django.http import HttpRequest, HttpResponse
from organizations.models import Membership, Organization


class OrganizationContextMiddleware:
    """Resolves and scopes the current active Organization from request headers.

    Headers checked:
      - X-Organization-ID (UUID)
      - X-Organization-Slug (string)

    Tenant safety invariant:
      Even if a client sends an organization ID or slug, the organization is ONLY
      attached to request.organization if the authenticated user has an active membership.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request.organization = None
        request.membership = None

        if hasattr(request, "user") and request.user.is_authenticated:
            org_id = request.headers.get("X-Organization-ID")
            org_slug = request.headers.get("X-Organization-Slug")

            membership = None
            if org_id:
                try:
                    uuid_val = uuid.UUID(org_id)
                    membership = Membership.objects.select_related("organization").filter(
                        organization__id=uuid_val,
                        organization__is_active=True,
                        user=request.user,
                        is_active=True,
                    ).first()
                except (ValueError, TypeError):
                    membership = None
            elif org_slug:
                membership = Membership.objects.select_related("organization").filter(
                    organization__slug=org_slug,
                    organization__is_active=True,
                    user=request.user,
                    is_active=True,
                ).first()

            if membership:
                request.membership = membership
                request.organization = membership.organization

        return self.get_response(request)

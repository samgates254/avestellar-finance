from django.urls import path
from organizations.views import (
    OrganizationDetailView,
    OrganizationListCreateView,
    OrganizationMemberDetailView,
    OrganizationMembersView,
)

urlpatterns = [
    path("organizations/", OrganizationListCreateView.as_view(), name="organization-list-create"),
    path("organizations/<uuid:id>/", OrganizationDetailView.as_view(), name="organization-detail"),
    path("organizations/<uuid:org_id>/members/", OrganizationMembersView.as_view(), name="organization-members"),
    path("organizations/<uuid:org_id>/members/<uuid:user_id>/", OrganizationMemberDetailView.as_view(), name="organization-member-detail"),
]

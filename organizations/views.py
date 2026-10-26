import uuid
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.utils import extend_schema, extend_schema_view

from identity.models import User
from organizations.models import Membership, Organization, RoleChoices
from organizations.permissions import (
    IsAccountantOrAbove,
    IsOrgAdminOrOwner,
    IsOrganizationMember,
    get_request_organization,
)
from organizations.serializers import (
    AddMemberSerializer,
    MembershipSerializer,
    OrganizationSerializer,
    UpdateMemberRoleSerializer,
)


@extend_schema_view(
    get=extend_schema(summary="List User Organizations", description="List all organizations where the current user has membership."),
    post=extend_schema(summary="Create Organization", description="Create a new organization and become its Owner."),
)
class OrganizationListCreateView(generics.ListCreateAPIView):
    serializer_class = OrganizationSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False) or not self.request.user.is_authenticated:
            return Organization.objects.none()
        return Organization.objects.filter(
            memberships__user=self.request.user,
            memberships__is_active=True,
            is_active=True,
        ).distinct()

    @transaction.atomic
    def perform_create(self, serializer):
        org = serializer.save()
        Membership.objects.create(
            organization=org,
            user=self.request.user,
            role=RoleChoices.OWNER,
            is_active=True,
        )
        try:
            from ledger.services.initialization import initialize_organization_accounts
            initialize_organization_accounts(org, self.request.user)
        except (ImportError, Exception):
            pass


@extend_schema_view(
    get=extend_schema(summary="Get Organization Details", description="Retrieve organization profile by ID."),
    patch=extend_schema(summary="Update Organization", description="Update organization settings (Owner or Admin only)."),
)
class OrganizationDetailView(generics.RetrieveUpdateAPIView):
    serializer_class = OrganizationSerializer
    permission_classes = (permissions.IsAuthenticated, IsOrgAdminOrOwner)
    lookup_field = "id"

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False) or not self.request.user.is_authenticated:
            return Organization.objects.none()
        return Organization.objects.filter(
            memberships__user=self.request.user,
            memberships__is_active=True,
            is_active=True,
        ).distinct()


@extend_schema_view(
    get=extend_schema(
        summary="List Organization Members",
        description="List all members in the organization.",
        responses={200: MembershipSerializer(many=True)},
    ),
    post=extend_schema(
        summary="Add Organization Member",
        description="Add an existing user by email to this organization.",
        request=AddMemberSerializer,
        responses={201: MembershipSerializer},
    ),
)
class OrganizationMembersView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsOrgAdminOrOwner)

    def get(self, request, org_id):
        org = get_request_organization(request, self)
        if not org:
            return Response({"detail": "Organization not found or access denied."}, status=status.HTTP_404_NOT_FOUND)
        members = Membership.objects.filter(organization=org, is_active=True).select_related("user")
        serializer = MembershipSerializer(members, many=True)
        return Response(serializer.data)

    def post(self, request, org_id):
        org = get_request_organization(request, self)
        if not org:
            return Response({"detail": "Organization not found or access denied."}, status=status.HTTP_404_NOT_FOUND)
        
        serializer = AddMemberSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"].lower()
        role = serializer.validated_data["role"]

        user = User.objects.filter(email=email).first()
        if not user:
            return Response(
                {"detail": f"User with email '{email}' does not exist."},
                status=status.HTTP_404_NOT_FOUND,
            )

        membership, created = Membership.objects.get_or_create(
            organization=org,
            user=user,
            defaults={"role": role, "is_active": True},
        )
        if not created:
            if not membership.is_active:
                membership.is_active = True
                membership.role = role
                membership.save(update_fields=["is_active", "role"])
            else:
                return Response(
                    {"detail": "User is already an active member of this organization."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        return Response(MembershipSerializer(membership).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    delete=extend_schema(summary="Remove Member", description="Remove member from organization.", responses={204: None}),
    patch=extend_schema(
        summary="Update Member Role",
        description="Update member role.",
        request=UpdateMemberRoleSerializer,
        responses={200: MembershipSerializer},
    ),
)
class OrganizationMemberDetailView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsOrgAdminOrOwner)

    def delete(self, request, org_id, user_id):
        org = get_request_organization(request, self)
        if not org:
            return Response({"detail": "Organization not found or access denied."}, status=status.HTTP_404_NOT_FOUND)
        
        membership = get_object_or_404(Membership, organization=org, user_id=user_id)
        if membership.role == RoleChoices.OWNER:
            owner_count = Membership.objects.filter(organization=org, role=RoleChoices.OWNER, is_active=True).count()
            if owner_count <= 1:
                return Response(
                    {"detail": "Cannot remove the only Owner of an organization."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        membership.is_active = False
        membership.save(update_fields=["is_active"])
        return Response(status=status.HTTP_204_NO_CONTENT)

    def patch(self, request, org_id, user_id):
        org = get_request_organization(request, self)
        if not org:
            return Response({"detail": "Organization not found or access denied."}, status=status.HTTP_404_NOT_FOUND)
        
        membership = get_object_or_404(Membership, organization=org, user_id=user_id)
        new_role = request.data.get("role")
        if new_role not in RoleChoices.values:
            return Response({"detail": f"Invalid role. Choices: {RoleChoices.values}"}, status=status.HTTP_400_BAD_REQUEST)
        
        membership.role = new_role
        membership.save(update_fields=["role"])
        return Response(MembershipSerializer(membership).data)

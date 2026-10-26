from rest_framework import serializers
from identity.models import User
from organizations.models import Membership, Organization, RoleChoices


class OrganizationSerializer(serializers.ModelSerializer):
    user_role = serializers.SerializerMethodField()

    class Meta:
        model = Organization
        fields = (
            "id",
            "name",
            "slug",
            "currency",
            "currency_symbol",
            "decimal_places",
            "fiscal_year_start_month",
            "is_active",
            "user_role",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "user_role", "created_at", "updated_at")

    def get_user_role(self, obj) -> str | None:
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            membership = obj.memberships.filter(user=request.user, is_active=True).first()
            return membership.role if membership else None
        return None


class MembershipSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source="user.email", read_only=True)
    user_name = serializers.CharField(source="user.full_name", read_only=True)

    class Meta:
        model = Membership
        fields = (
            "id",
            "organization",
            "user",
            "user_email",
            "user_name",
            "role",
            "is_active",
            "created_at",
        )
        read_only_fields = ("id", "organization", "created_at")


class AddMemberSerializer(serializers.Serializer):
    email = serializers.EmailField()
    role = serializers.ChoiceField(choices=RoleChoices.choices, default=RoleChoices.ACCOUNTANT)


class UpdateMemberRoleSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=RoleChoices.choices)

from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework_simplejwt.views import TokenObtainPairView
from drf_spectacular.utils import extend_schema, extend_schema_view
from identity.models import User
from identity.serializers import (
    CustomTokenObtainPairSerializer,
    RegisterSerializer,
    UserSerializer,
)


@extend_schema_view(
    post=extend_schema(
        summary="User Authentication (JWT)",
        description="Obtain JWT access and refresh token pair with email and password.",
    )
)
class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer


@extend_schema_view(
    post=extend_schema(
        summary="Register New User",
        description="Create a new user account with email, name, and password.",
    )
)
class RegisterView(generics.CreateAPIView):
    serializer_class = RegisterSerializer
    permission_classes = (permissions.AllowAny,)


@extend_schema_view(
    get=extend_schema(
        summary="Current User Profile",
        description="Get authenticated user details.",
    ),
    patch=extend_schema(
        summary="Update Current User Profile",
        description="Update profile fields of the authenticated user.",
    ),
)
class UserProfileView(generics.RetrieveUpdateAPIView):
    serializer_class = UserSerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_object(self):
        return self.request.user

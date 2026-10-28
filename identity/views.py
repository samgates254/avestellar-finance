from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView
from drf_spectacular.utils import extend_schema, extend_schema_view
from identity.models import User
from identity.serializers import (
    CustomTokenObtainPairSerializer,
    LogoutSerializer,
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
    # Throttle failed/successful login attempts to blunt credential stuffing.
    # Requires REDIS_URL for correct behaviour across multiple workers; see
    # config/settings.py CACHES note.
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "login"


@extend_schema_view(
    post=extend_schema(
        summary="Register New User",
        description="Create a new user account with email, name, and password.",
    )
)
class RegisterView(generics.CreateAPIView):
    serializer_class = RegisterSerializer
    permission_classes = (permissions.AllowAny,)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "register"


@extend_schema_view(
    post=extend_schema(
        summary="Logout (revoke refresh token)",
        description=(
            "Blacklist the supplied refresh token so it can no longer be used to "
            "mint new access tokens. The token must belong to the authenticated "
            "user. The access token is short-lived and simply expires; the client "
            "is responsible for discarding both. Repeating the request is safe "
            "and returns the same 205 response."
        ),
    )
)
class LogoutView(generics.GenericAPIView):
    permission_classes = (permissions.IsAuthenticated,)
    serializer_class = LogoutSerializer

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        raw_refresh = serializer.validated_data["refresh"]
        try:
            token = RefreshToken(raw_refresh)
        except TokenError:
            # Malformed, expired, or already blacklisted: there is nothing left to
            # revoke. Logout is deliberately idempotent so a client can always
            # discard its session, and the response never echoes token contents.
            return Response({"detail": "Logged out."}, status=status.HTTP_205_RESET_CONTENT)

        # Never revoke a structurally valid token that belongs to another user.
        if str(token.get("user_id")) != str(request.user.pk):
            return Response(
                {"detail": "This refresh token does not belong to the authenticated user."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # blacklist() records the token in the token_blacklist tables, so any
        # subsequent refresh with this value is rejected.
        try:
            token.blacklist()
        except TokenError:
            pass  # raced with a concurrent logout; already revoked.
        return Response({"detail": "Logged out."}, status=status.HTTP_205_RESET_CONTENT)


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

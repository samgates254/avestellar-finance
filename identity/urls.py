from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView
from identity.views import (
    CustomTokenObtainPairView,
    LogoutView,
    RegisterView,
    UserProfileView,
)

urlpatterns = [
    path("auth/register/", RegisterView.as_view(), name="auth-register"),
    path("auth/token/", CustomTokenObtainPairView.as_view(), name="token-obtain-pair"),
    path("auth/token/refresh/", TokenRefreshView.as_view(), name="token-refresh"),
    path("auth/logout/", LogoutView.as_view(), name="auth-logout"),
    path("auth/me/", UserProfileView.as_view(), name="user-profile"),
]

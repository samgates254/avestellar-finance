from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)
from config.views import HealthCheckView

# Version 1 API URLs
api_v1_patterns = [
    path("health/", HealthCheckView.as_view(), name="health-check"),
    path("", include("identity.urls")),
    path("", include("organizations.urls")),
    path("", include("ledger.urls")),
    path("", include("invoicing.urls")),
    path("", include("purchases.urls")),
    path("", include("payments.urls")),
    path("", include("automation.urls")),
    path("", include("integrations.urls")),
    path("", include("analytics.urls")),
]

urlpatterns = [
    path("admin/", admin.site.urls),
    # OpenAPI Schema & Docs
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
    # API Version 1
    path("api/v1/", include((api_v1_patterns, "api_v1"))),
]

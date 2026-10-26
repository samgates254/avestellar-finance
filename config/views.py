from django.db import connection
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema


class HealthCheckView(APIView):
    """System health check endpoint verifying database connectivity and operational status."""
    permission_classes = (permissions.AllowAny,)

    @extend_schema(
        summary="System Health Check",
        description="Returns system health and database connectivity state.",
        responses={200: OpenApiTypes.OBJECT, 503: OpenApiTypes.OBJECT},
    )
    def get(self, request):
        db_healthy = True
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
        except Exception:
            db_healthy = False

        status_code = status.HTTP_200_OK if db_healthy else status.HTTP_503_SERVICE_UNAVAILABLE
        return Response(
            {
                "status": "HEALTHY" if db_healthy else "UNHEALTHY",
                "database": "CONNECTED" if db_healthy else "DISCONNECTED",
                "system": "AVESTELLAR FINANCE API",
                "version": "1.0.0",
                "timestamp": timezone.now().isoformat(),
            },
            status=status_code,
        )

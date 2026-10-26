import hashlib
import json
from functools import wraps
from rest_framework import status
from rest_framework.response import Response

from automation.models import IdempotencyRecord
from organizations.permissions import get_request_organization


def idempotent_action(view_func):
    """Decorator for DRF API views to ensure idempotent execution for mutating requests."""
    @wraps(view_func)
    def wrapper(view_instance, request, *args, **kwargs):
        idempotency_key = request.headers.get("Idempotency-Key") or request.headers.get("X-Idempotency-Key")
        if not idempotency_key:
            return view_func(view_instance, request, *args, **kwargs)

        org = get_request_organization(request, view_instance)
        if not org:
            return view_func(view_instance, request, *args, **kwargs)

        # Hash request body
        try:
            body_bytes = request.body
        except Exception:
            body_bytes = b""
        request_hash = hashlib.sha256(body_bytes).hexdigest()

        existing = IdempotencyRecord.objects.filter(organization=org, key=idempotency_key).first()
        if existing:
            if existing.request_hash != request_hash:
                return Response(
                    {"detail": "Idempotency key reused with different request payload."},
                    status=status.HTTP_409_CONFLICT,
                )
            return Response(existing.response_body, status=existing.response_status)

        # Execute original view
        response = view_func(view_instance, request, *args, **kwargs)

        # If successful or client error, save idempotency record
        if 200 <= response.status_code < 500:
            try:
                # Ensure data is serializable
                data = response.data if hasattr(response, "data") else {}
                IdempotencyRecord.objects.create(
                    organization=org,
                    key=idempotency_key,
                    request_path=request.path,
                    request_hash=request_hash,
                    response_status=response.status_code,
                    response_body=data,
                )
            except Exception:
                pass

        return response

    return wrapper

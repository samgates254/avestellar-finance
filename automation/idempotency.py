import hashlib
from functools import wraps

from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.response import Response

from automation.models import IdempotencyRecord
from organizations.permissions import get_request_organization


class _ReplayExisting(Exception):
    """Internal signal: the key was already reserved by another (committed) request."""

    def __init__(self, record):
        self.record = record
        super().__init__("idempotency key already reserved")


def _matches(record, request_hash, request_path, request_method, actor):
    """A stored key may only be replayed for the exact same operation and payload.

    Scoping on organization (enforced by the query filter and the unique
    constraint), the authenticated actor, the request operation (path + method)
    and the payload hash means a key can never leak another tenant's result or
    silently substitute a different request.
    """
    return (
        record.request_hash == request_hash
        and record.request_path == request_path
        and record.request_method == request_method
        and record.actor == actor
    )


def idempotent_action(view_func):
    """Make a DRF mutating view replay-safe against network retries and races.

    Behaviour:
    * No ``Idempotency-Key``/``X-Idempotency-Key`` header (or no organization
      context) → the operation runs unchanged, so existing callers are unaffected.
    * The reservation row and the financial writes are committed in a SINGLE
      transaction, so an interrupted or failed transaction never leaves a key
      marked successful — the key is free again for a corrected retry.
    * Concurrent requests for the same ``(organization, key)`` are serialized by
      the database unique constraint: the loser blocks on the index, and once the
      winner commits it replays the original result instead of re-running.
    * Reusing a key for a different organization/operation/actor/payload is
      rejected with ``409 Conflict`` rather than silently returning a mismatched
      result.
    * Authorization and validation still run: permission checks execute in
      ``dispatch()`` before the wrapped method, and any serializer/validation
      error propagates so the reservation is rolled back.
    """

    @wraps(view_func)
    def wrapper(view_instance, request, *args, **kwargs):
        idempotency_key = request.headers.get("Idempotency-Key") or request.headers.get("X-Idempotency-Key")
        if not idempotency_key:
            return view_func(view_instance, request, *args, **kwargs)

        org = get_request_organization(request, view_instance)
        if not org:
            return view_func(view_instance, request, *args, **kwargs)

        try:
            body_bytes = request.body
        except Exception:
            body_bytes = b""
        request_hash = hashlib.sha256(body_bytes).hexdigest()
        request_path = request.path
        request_method = request.method
        actor = str(getattr(getattr(request, "user", None), "id", "") or "")

        try:
            with transaction.atomic():
                try:
                    # The savepoint isolates the INSERT's IntegrityError so the
                    # enclosing transaction stays usable for the replay lookup.
                    with transaction.atomic():
                        record = IdempotencyRecord.objects.create(
                            organization=org,
                            key=idempotency_key,
                            request_path=request_path,
                            request_method=request_method,
                            request_hash=request_hash,
                            actor=actor,
                            response_status=0,
                            response_body={},
                        )
                except IntegrityError:
                    existing = (
                        IdempotencyRecord.objects
                        .filter(organization=org, key=idempotency_key)
                        .first()
                    )
                    if existing is None:
                        # The winning transaction aborted after our blocked
                        # insert; run the operation normally instead of
                        # deadlocking on the reservation.
                        return view_func(view_instance, request, *args, **kwargs)
                    raise _ReplayExisting(existing)

                response = view_func(view_instance, request, *args, **kwargs)

                # Persist the result inside the same transaction as the writes.
                record.response_status = response.status_code
                record.response_body = _jsonable(getattr(response, "data", None))
                record.save(update_fields=["response_status", "response_body"])
                return response
        except _ReplayExisting as replay:
            existing = replay.record
            if not _matches(existing, request_hash, request_path, request_method, actor):
                return Response(
                    {"detail": "Idempotency key has already been used for a different request."},
                    status=status.HTTP_409_CONFLICT,
                )
            replayed_status = existing.response_status or status.HTTP_200_OK
            return Response(existing.response_body, status=replayed_status)

    return wrapper


def _jsonable(data):
    """Store only JSON-native response content in the idempotency record."""
    if data is None:
        return {}
    if isinstance(data, (dict, list, str, int, float, bool)):
        return data
    return {
        "detail": "The original response could not be stored for replay.",
        "unserializable_type": type(data).__name__,
    }

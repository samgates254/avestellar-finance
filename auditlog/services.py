from typing import Any
from auditlog.models import AuditLog


def record_audit_log(
    action: str,
    target_model: str,
    target_id: str | Any,
    organization=None,
    user=None,
    details: dict | None = None,
    ip_address: str | None = None,
) -> AuditLog:
    """Helper to record an audit log entry."""
    return AuditLog.objects.create(
        organization=organization,
        user=user,
        action=action,
        target_model=target_model,
        target_id=str(target_id),
        details=details or {},
        ip_address=ip_address,
    )

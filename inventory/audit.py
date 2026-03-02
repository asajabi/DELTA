from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from .models import AuditLog, Branch, UserProfile


def _to_json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _to_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_json_safe(v) for v in value]
    return value


def _actor_employee_id(actor) -> str:
    if not actor or not getattr(actor, "is_authenticated", False):
        return ""
    try:
        profile = actor.profile
    except UserProfile.DoesNotExist:
        return ""
    return profile.employee_id or ""


def log_audit_event(
    *,
    actor=None,
    request=None,
    ip_address: str | None = None,
    action: str,
    reason: str,
    object_type: str,
    object_id: str | int | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    branch: Branch | None = None,
) -> AuditLog:
    clean_reason = (reason or "").strip()
    if not clean_reason:
        raise ValueError("Audit reason is required.")

    actor_user = actor if actor and getattr(actor, "is_authenticated", False) else None
    after_payload = _to_json_safe(after or {})
    if isinstance(after_payload, dict):
        after_payload.setdefault("reason", clean_reason)
    resolved_ip = ip_address
    if not resolved_ip and request is not None:
        resolved_ip = (
            request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
            or request.META.get("REMOTE_ADDR", "")
        )

    return AuditLog.objects.create(
        actor=actor_user,
        actor_username=actor_user.username if actor_user else "SYSTEM",
        actor_employee_id=_actor_employee_id(actor_user),
        action=action,
        reason=clean_reason,
        object_type=object_type,
        model_name=object_type,
        object_id=str(object_id or ""),
        ip_address=resolved_ip or None,
        branch=branch,
        before_data=_to_json_safe(before or {}),
        after_data=after_payload,
        old_values=_to_json_safe(before or {}),
        new_values=after_payload,
    )

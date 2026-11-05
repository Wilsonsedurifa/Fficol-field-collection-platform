"""Append small allowlisted audit evidence; never copy an entire request."""

from django.core.exceptions import ValidationError

from .models import AuditEvent


ALLOWED_CHANGE_FIELDS = {
    "backoffice_invitation_sent": {"status"},
    "backoffice_invitation_accepted": {"status"},
    "backoffice_module_access_changed": {"module", "enabled"},
    "information_proposal_submitted": {"category", "status"},
    "information_proposal_approved": {"category", "status"},
    "information_proposal_rejected": {"category", "status"},
    "account_created": {"status", "source_system", "reference"},
    "address_verified": {"verification_status"},
    "commission_rule_created": {"code", "version", "kind"},
    "collector_availability_changed": {"availability"},
    "offer_accepted": {"status"},
    "offer_declined": {"status"},
    "offer_created": {"status"},
    "offer_withdrawn": {"status"},
    "visit_started": {"status"},
    "visit_submitted": {"status"},
    "visit_reviewed": {"review_status"},
    "visit_cancelled": {"status"},
    "contact_attempt_recorded": {"status", "kind", "outcome"},
    "revisit_scheduled": {"status"},
    "followup_requested": {"status"},
    "followup_rejected": {"status"},
    "followup_authorized": {"status", "available_from", "due_at"},
    "revisit_completed": {"status"},
    "payment_reported": {"status", "amount", "method"},
    "payment_verified": {"status"},
    "payment_rejected": {"status"},
    "payment_reversed": {"status"},
    "document_uploaded": {"kind", "size_bytes"},
    "release_requested": {"status"},
    "release_approved": {"status"},
    "release_rejected": {"status"},
    "release_superseded": {"status", "reason"},
    "assignment_released": {"status"},
    "assignment_reassigned": {"status", "reason"},
    "account_assigned": {"status", "new_assignment_id", "collector", "reason", "withdrawn_offers_count"},
    "account_reassigned": {"status", "previous_assignment_id", "new_assignment_id", "previous_collector", "new_collector", "reason", "cancelled_followups_count", "superseded_release_request_id", "withdrawn_offers_count"},
    "followup_cancelled": {"status"},
    "earning_rule_published": {"category", "version", "amount"},
    "commission_created": {"category", "amount", "status"},
    "commission_approved": {"status"},
    "commission_rejected": {"status", "reason"},
    "payout_created": {"status", "item_count", "amount"},
    "payout_approved": {"status"},
    "payout_paid": {"status", "reference"},
    "point_rule_published": {"category", "version", "points"},
    "point_awarded": {"category", "points"},
    "account_status_changed": {"status", "from_status", "to_status", "reason"},
    "assignment_completed": {"status", "reason"},
    "promise_cancelled": {"status"},
    "system_admin_user_created": {"is_active", "collector_profile_created", "role_group_count", "invitation_sent"},
    "system_admin_user_updated": {
        "collector_account_disabled",
        "is_active",
        "backoffice_permission_count",
        "active_client_access_count",
        "collector_profile_created",
        "role_group_count",
    },
    "system_admin_password_reset": {"password_reset"},
    "system_admin_collector_status_changed": {"approval_status", "reason", "note"},
    "system_admin_collector_converted": {"collector_status", "role_group_count"},
    "collector_invitation_sent": {"status", "email"},
    "collector_invitation_resent": {"status", "email"},
    "collector_invitation_revoked": {"status"},
    "collector_invitation_accepted": {"status"},
    "collector_suspended": {"approval_status", "reason"},
    "collector_reinstated": {"approval_status", "note"},
    "collector_offboarded": {"approval_status", "reason"},
    "collector_warning_issued": {"level", "reason"},
    "collector_password_reset_sent": {"status", "email"},
    "collector_email_changed": {"old_email", "new_email", "email_verified_cleared"},
    "system_admin_client_created": {"code", "is_active"},
    "system_admin_client_updated": {"code", "is_active", "name_changed"},
}


def validate_audit_changes(action, changes):
    if action not in ALLOWED_CHANGE_FIELDS or not isinstance(changes, dict):
        raise ValidationError("Use a supported audit action and a change dictionary.")
    if not set(changes).issubset(ALLOWED_CHANGE_FIELDS[action]):
        raise ValidationError("Audit evidence contains fields not allowed for this action.")
    if action in {"system_admin_user_created", "system_admin_user_updated"} and "collector_profile_created" in changes:
        if type(changes["collector_profile_created"]) is not bool:
            raise ValidationError("Audit values must be bounded scalar before/after evidence.")
    for value in changes.values():
        values = value.values() if isinstance(value, dict) and set(value) <= {"before", "after"} else [value]
        for item in values:
            if item is not None and not isinstance(item, (str, int, bool)):
                raise ValidationError("Audit values must be bounded scalar before/after evidence.")
            if isinstance(item, str) and len(item) > 100:
                raise ValidationError("Audit text exceeds the approved evidence length.")
    return changes


def append_audit_event(*, actor, action, entity_type, entity_id, changes=None, account=None, request_id=None):
    """Call inside the transaction that makes the authorized business change."""
    changes = validate_audit_changes(action, {} if changes is None else changes)
    event = AuditEvent(
        actor=actor, account=account, action=action, entity_type=entity_type,
        entity_id=str(entity_id), changes=changes, request_id=request_id,
    )
    # JSONField treats {} as blank during model validation; the dedicated
    # allowlist above accepts that legitimate empty evidence object.
    event.full_clean(exclude=["changes"])
    event.save(force_insert=True)
    return event

"""Orchestration and validation for account status transitions."""

from functools import wraps
import time
from decimal import Decimal
from uuid import UUID

from django.db import OperationalError, transaction
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.accounts.models import Account, AccountStatusChange, Client
from apps.assignments.models import AccountAssignment
from apps.audit.services import append_audit_event
from apps.collection.services import has_unverified_payments
from apps.core.notification_services import notify_user
from apps.user.models import ClientAccess, CollectorProfile

from .constants import (
    ALLOWED_STATUS_TRANSITIONS,
    BUSY,
    CLIENT_ACCESS_REQUIRED,
    EVENT_CONFLICT,
    INVALID_STATUS_TRANSITION,
    PAUSED_STATUSES,
    PERMISSION_REQUIRED,
    REASON_REQUIRED,
    TERMINAL_STATUSES,
    UNVERIFIED_PAYMENTS_EXIST,
    ZERO_BALANCE_REQUIRED,
)
from .effects import apply_status_transition_side_effects
from .exceptions import AccountStatusWorkflowError


def _retry_transaction(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        for attempt in range(3):
            try:
                return function(*args, **kwargs)
            except OperationalError as exc:
                code = getattr(exc.__cause__, "sqlstate", None)
                if code not in {"40001", "40P01"}:
                    raise
                if attempt == 2:
                    raise AccountStatusWorkflowError(BUSY, "Please retry; another update is in progress.", 409) from exc
                time.sleep(0.05 * (attempt + 1))
    return wrapped


def _validate_actor(actor):
    if not actor.is_authenticated or not actor.is_active or not actor.has_perm("accounts.change_account_status"):
        raise AccountStatusWorkflowError(PERMISSION_REQUIRED, "Account status change permission is required.", 403)


def _validate_grant(client, grant):
    if grant is None or not grant.is_active or not client.is_active:
        raise AccountStatusWorkflowError(CLIENT_ACCESS_REQUIRED, "Active client access is required for this operation.", 403)


def _validate_reason(reason):
    if not isinstance(reason, str) or not reason.strip():
        raise AccountStatusWorkflowError(REASON_REQUIRED, "A non-blank reason is required.", 400)
    trimmed = reason.strip()
    if len(trimmed) > 5000:
        raise AccountStatusWorkflowError(REASON_REQUIRED, "Reason must not exceed 5000 characters.", 400)
    return trimmed


def _validate_transition(from_status, to_status, account):
    allowed = ALLOWED_STATUS_TRANSITIONS.get(from_status, frozenset())
    if to_status not in allowed:
        raise AccountStatusWorkflowError(
            INVALID_STATUS_TRANSITION,
            f"Cannot transition account from '{from_status}' to '{to_status}'.",
            400,
        )
    if to_status in {Account.Status.RESOLVED, Account.Status.CLOSED}:
        if has_unverified_payments(account):
            raise AccountStatusWorkflowError(
                UNVERIFIED_PAYMENTS_EXIST,
                "Cannot resolve or close an account with unverified payment reports. Verify or reject all pending payments first.",
                409,
            )
        if account.balance_snapshot != Decimal("0.00"):
            raise AccountStatusWorkflowError(
                ZERO_BALANCE_REQUIRED,
                "Cannot resolve or close this account until its current source balance snapshot is 0.00.",
                409,
            )


def _notify_collector(collector_user, account, to_status, reason, event_id):
    if collector_user is None:
        return
    end_state = "completed" if to_status in TERMINAL_STATUSES else "released"
    message = (
        f"Account {account.reference} status changed to {to_status}. "
        f"Your assignment has been {end_state}. Reason: {reason}"
    )[:300]
    user_id = getattr(collector_user, "pk", collector_user)
    event_key = f"status-change-{event_id}"
    notify_user(
        user_id=user_id,
        account=account,
        event_key=event_key,
        message=message,
        kind="system",
    )


@_retry_transaction
def change_account_status(account_id, to_status, reason, actor, event_id):
    """Transition an account status with automatic side effects and audit trail.

    Lock order matches collector workflows: Account -> Profile -> Assignment -> Client -> Grant.
    """
    clean_reason = _validate_reason(reason)
    try:
        clean_event_id = UUID(str(event_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise AccountStatusWorkflowError("invalid_event_id", "Provide a valid event ID.", 400) from exc

    with transaction.atomic():
        account = Account.objects.select_for_update(of=("self",)).filter(pk=account_id).first()
        if account is None:
            raise AccountStatusWorkflowError("account_not_found", "Account not found.", 404)
        assignment_lookup = AccountAssignment.objects.filter(account=account, status=AccountAssignment.Status.ACTIVE).first()
        collector_user = None
        if assignment_lookup:
            profile = CollectorProfile.objects.select_for_update().select_related("user").get(pk=assignment_lookup.collector_id)
            collector_user = profile.user
        assignment = AccountAssignment.objects.select_for_update(of=("self",)).filter(
            account=account, status=AccountAssignment.Status.ACTIVE
        ).first()
        client = Client.objects.select_for_update().get(pk=account.client_id)
        actor = get_user_model().objects.filter(pk=getattr(actor, "pk", None), is_active=True).first()
        if actor is None:
            raise AccountStatusWorkflowError(PERMISSION_REQUIRED, "An active authorized user is required.", 403)
        _validate_actor(actor)

        grant = ClientAccess.objects.select_for_update(of=("self",)).filter(
            user=actor, client=client, is_active=True
        ).first()
        _validate_grant(client, grant)

        existing = AccountStatusChange.objects.select_for_update().filter(event_id=clean_event_id).first()
        if existing is not None:
            if existing.account_id == account.pk and existing.to_status == to_status and existing.reason == clean_reason and existing.changed_by_id == actor.pk:
                return {"saved": True, "created": False, "status_change": existing}
            raise AccountStatusWorkflowError(EVENT_CONFLICT, "This event ID has already been used with conflicting data.", 409)

        _validate_grant(client, grant)
        from_status = account.status
        _validate_transition(from_status, to_status, account)

        now = timezone.now()
        apply_status_transition_side_effects(
            account=account,
            assignment=assignment,
            to_status=to_status,
            reason=clean_reason,
            reviewer=actor,
            now=now,
        )

        account.status = to_status
        account.save(update_fields=["status", "updated_at"])

        status_change = AccountStatusChange.objects.create(
            account=account,
            from_status=from_status,
            to_status=to_status,
            reason=clean_reason,
            changed_by=actor,
            event_id=clean_event_id,
        )

        append_audit_event(
            actor=actor,
            action="account_status_changed",
            entity_type="Account",
            entity_id=account.pk,
            account=account,
            changes={
                "status": {"before": from_status, "after": to_status},
                "from_status": from_status,
                "to_status": to_status,
                "reason": clean_reason[:100],
            },
        )

        if to_status in TERMINAL_STATUSES or to_status in PAUSED_STATUSES:
            _notify_collector(collector_user, account, to_status, clean_reason, clean_event_id)

        # TODO(wired-crm): sync status change to WIRED CRM when account status changes.
        return {"saved": True, "created": True, "status_change": status_change}

"""Validate earning terms without assuming company-approved rates or payouts.
Manage non-custodial payment reporting and independent verification.
"""

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone as utc_timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID, uuid4

from django.conf import settings
from django.core.files.storage import default_storage
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import FileResponse, Http404
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.accounts.models import Account, Client
from apps.activities.models import Document, FieldActivity
from apps.assignments.models import AccountAssignment
from functools import wraps
import time
from django.db import OperationalError

from apps.audit.services import append_audit_event
from apps.user.models import ClientAccess, CollectorProfile, has_backoffice_access

from .models import (
    Commission,
    CommissionRule,
    InformationCategory,
    InformationEarningRule,
    Payment,
    Payout,
    PayoutItem,
    PointRule,
    PointTransaction,
    PromiseToPay,
)


class CollectionWorkflowError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


OfferWorkflowError = CollectionWorkflowError


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
                    raise CollectionWorkflowError("busy", "Please retry; another update is in progress.", 409) from exc
                time.sleep(0.05 * (attempt + 1))
    return wrapped


def _get_collector_profile(user_id, lock=False):
    from apps.assignments.services import collector_profile
    return collector_profile(user_id, lock=lock)


def _error(message, code="invalid_payment", status=400):
    raise CollectionWorkflowError(code, message, status)


def _text(value, label, maximum, required=False):
    if not isinstance(value, str) or len(value) > maximum:
        _error(f"{label} must be text of no more than {maximum} characters.")
    value = value.strip()
    if required and not value:
        _error(f"Provide {label.lower()}.")
    return value


def _datetime(value):
    try:
        result = parse_datetime(value) if isinstance(value, str) else None
        if result is not None and timezone.is_aware(result):
            return result.astimezone(utc_timezone.utc)
    except (ValueError, OverflowError):
        pass
    _error("Provide a valid date and time including its timezone.")


# ---------------------------------------------------------------------------
# Commission terms validation (existing baseline)
# ---------------------------------------------------------------------------

def validate_commission_terms(rule, tiers=None):
    """Caller must lock the rule during publication or offer snapshotting.

    This validates coverage but does not publish or mutate a rule. Once used,
    later workflow services must preserve it and create a new version instead.
    """
    rule.clean_fields(exclude=["client", "created_at", "updated_at"])
    rule.clean()
    bands = list(rule.tiers.order_by("lower_amount")) if tiers is None else list(tiers)
    if rule.kind != CommissionRule.Kind.TIERED:
        if bands:
            raise ValidationError("Only tiered earning rules may contain amount bands.")
        return rule
    if not bands:
        raise ValidationError("Tiered rules require bands covering supported amounts.")
    if any(band.lower_amount is None for band in bands):
        raise ValidationError("Each band requires a lower amount.")
    bands.sort(key=lambda band: band.lower_amount)
    next_lower = Decimal("0")
    for index, band in enumerate(bands):
        band.clean_fields(exclude=["rule", "created_at", "updated_at"])
        if band.rule_id != rule.pk:
            raise ValidationError("All bands must belong to the same earning-rule version.")
        if band.lower_amount != next_lower:
            raise ValidationError("Bands must cover amounts from zero without gaps or overlap.")
        if band.upper_amount is None:
            if index != len(bands) - 1:
                raise ValidationError("Only the final band may be unbounded.")
        elif band.upper_amount <= band.lower_amount:
            raise ValidationError("Each upper bound must exceed its lower bound.")
        next_lower = band.upper_amount
    if next_lower is not None:
        raise ValidationError("The final band must cover amounts above its lower bound.")
    return rule


# ---------------------------------------------------------------------------
# Private Document Upload Validation & Storage
# ---------------------------------------------------------------------------

MAGIC_SIGNATURES = [
    (b"%PDF-", "application/pdf"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
]

ALLOWED_EXTENSIONS = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}


def _detect_magic_bytes(header):
    for signature, mime in MAGIC_SIGNATURES:
        if header.startswith(signature):
            return mime
    if len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp"
    return None


def validate_uploaded_document(file_obj):
    """Multi-layer validation of proof document: size, extension, magic bytes, hash."""
    if file_obj is None:
        _error("Proof of payment file is required.", "missing_document", 400)

    max_size = getattr(settings, "DOCUMENT_MAX_UPLOAD_SIZE", 5 * 1024 * 1024)
    if file_obj.size < 100:
        _error("Uploaded file is empty or corrupted.", "invalid_file_size", 400)
    if file_obj.size > max_size:
        _error(f"Uploaded file exceeds the maximum size limit of {max_size // (1024 * 1024)} MB.", "file_too_large", 413)

    raw_name = getattr(file_obj, "name", "") or ""
    clean_name = os.path.basename(raw_name).strip()
    clean_name = re.sub(r"[^\w\s.-]", "_", clean_name)
    if not clean_name:
        clean_name = "proof_of_payment.bin"
    ext = os.path.splitext(clean_name)[1].lower()

    if ext not in ALLOWED_EXTENSIONS:
        _error("Unsupported file extension. Only JPG, PNG, WebP, and PDF are allowed.", "unsupported_media_type", 415)

    expected_mime = ALLOWED_EXTENSIONS[ext]

    # Inspect magic bytes from beginning
    file_obj.seek(0)
    header = file_obj.read(16)
    detected_mime = _detect_magic_bytes(header)
    if not detected_mime or detected_mime != expected_mime:
        _error("File content does not match its declared format.", "invalid_file_content", 415)

    # Compute SHA-256
    file_obj.seek(0)
    hasher = hashlib.sha256()
    for chunk in file_obj.chunks(chunk_size=65536):
        hasher.update(chunk)
    sha256 = hasher.hexdigest().lower()

    file_obj.seek(0)
    return {
        "original_name": clean_name[:255],
        "mime_type": expected_mime,
        "size_bytes": file_obj.size,
        "sha256": sha256,
        "extension": ext,
    }


def save_private_document(account_id, file_obj, meta):
    """Save evidence through the configured private storage backend."""
    safe_filename = f"{meta['sha256'][:16]}_{uuid4().hex[:8]}{meta['extension']}"
    file_obj.seek(0)
    relative_path = f"evidence/{account_id}/{safe_filename}"
    return default_storage.save(relative_path, file_obj)


# ---------------------------------------------------------------------------
# Payment Reporting Service (Collector)
# ---------------------------------------------------------------------------

def _fingerprint_payment(account_id, amount, paid_at_iso, method, reference, doc_sha256):
    payload = {
        "account_id": account_id,
        "amount": str(amount),
        "paid_at": paid_at_iso,
        "method": method,
        "reference": reference,
        "doc_sha256": doc_sha256,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_payment_report(payload, file_obj, now):
    if not isinstance(payload, dict):
        _error("Submit a valid payment report.")

    required_keys = {"event_id", "amount", "paid_at", "method"}
    allowed_keys = required_keys | {"reference", "notes", "promise_id", "activity_id"}
    if not required_keys.issubset(payload) or not set(payload).issubset(allowed_keys):
        _error("Provide the required payment fields only.")

    try:
        event_id = UUID(str(payload["event_id"]))
    except (ValueError, TypeError, AttributeError):
        _error("Provide a valid event ID for this payment.")

    try:
        raw_amount = str(payload["amount"]).strip()
        if len(raw_amount) > 20:
            raise ValueError
        amount = Decimal(raw_amount)
        if not amount.is_finite() or amount < Decimal("0.01") or amount > Decimal("999999999999.99") or amount != amount.quantize(Decimal("0.01")):
            raise ValueError
    except (ValueError, TypeError, InvalidOperation):
        _error("Enter a valid positive payment amount with at most two decimal places.")

    paid_at = _datetime(payload["paid_at"])
    # Allow 120s clock skew into future
    if paid_at > now + timedelta(seconds=120):
        _error("Payment date and time cannot be in the future.")

    method = str(payload["method"]).strip()
    if method not in Payment.Method.values:
        _error("Choose a supported payment method.")

    reference = _text(payload.get("reference", ""), "Payment reference", 100)
    notes = _text(payload.get("notes", ""), "Payment notes", 5000)

    promise_id = payload.get("promise_id")
    if promise_id is not None:
        try:
            promise_id = int(promise_id)
        except (ValueError, TypeError):
            _error("Invalid promise reference.")

    activity_id = payload.get("activity_id")
    if activity_id is not None:
        try:
            activity_id = int(activity_id)
        except (ValueError, TypeError):
            _error("Invalid activity reference.")

    doc_meta = validate_uploaded_document(file_obj)

    return {
        "event_id": event_id,
        "amount": amount,
        "paid_at": paid_at,
        "method": method,
        "reference": reference,
        "notes": notes,
        "promise_id": promise_id,
        "activity_id": activity_id,
        "doc_meta": doc_meta,
    }


def payment_payload(payment, document=None):
    if document is None and hasattr(payment, "documents"):
        document = payment.documents.first()
    doc_payload = None
    if document:
        doc_payload = {
            "id": document.pk,
            "original_name": document.original_name,
            "mime_type": document.mime_type,
            "size_bytes": document.size_bytes,
            "download_url": f"/documents/{document.pk}/download/",
        }
    return {
        "id": payment.pk,
        "event_id": str(payment.event_id),
        "amount": str(payment.amount),
        "currency": payment.account.currency,
        "paid_at": payment.paid_at.isoformat(),
        "method": payment.method,
        "method_display": payment.get_method_display(),
        "reference": payment.reference,
        "status": payment.status,
        "status_display": payment.get_status_display(),
        "notes": payment.notes,
        "document": doc_payload,
        "rejection_reason": payment.rejection_reason if payment.status == "rejected" else "",
    }


@_retry_transaction
def report_payment(user_id, assignment_id, payload, file_obj):
    """Record customer payment proof reported by a collector.

    Invariants:
    - Non-custodial: Collectors NEVER receive customer money through FiCol.
    - Does NOT mutate Account.balance_snapshot.
    - Does NOT change Account.status (no auto-close).
    - Does NOT fulfill PromiseToPay.
    - Does NOT generate Commission or PayoutItem.
    """
    now = timezone.now()
    validated = validate_payment_report(payload, file_obj, now)

    user = get_user_model().objects.filter(pk=user_id, is_active=True).first()
    if user is None:
        _error("Authentication required.", "authentication_required", 401)
    if has_backoffice_access(user):
        _error("Use the Back Office portal with this account.", "collector_portal_forbidden", 403)

    lookup = AccountAssignment.objects.filter(pk=assignment_id, collector__user_id=user_id).values("account_id").first()
    if lookup is None:
        _error("Assigned account not found.", "assignment_not_found", 404)

    with transaction.atomic():
        # Parent lock order: Account -> CollectorProfile -> Assignment
        account = Account.objects.select_for_update(of=("self",)).select_related("client").get(pk=lookup["account_id"])
        profile = _get_collector_profile(user_id, lock=True)
        assignment = AccountAssignment.objects.select_for_update(of=("self",)).filter(
            pk=assignment_id, account_id=account.pk, collector=profile, status="active"
        ).first()
        account.client = Client.objects.select_for_update().get(pk=account.client_id)
        grant = ClientAccess.objects.select_for_update(of=("self",)).filter(
            user_id=user_id, client_id=account.client_id, is_active=True
        ).first()

        if assignment is None or grant is None or not account.client.is_active:
            _error("This account is no longer available to you.", "assignment_unavailable", 404)

        fingerprint = _fingerprint_payment(
            account.pk,
            validated["amount"],
            validated["paid_at"].isoformat(),
            validated["method"],
            validated["reference"],
            validated["doc_meta"]["sha256"],
        )

        # Idempotency check on event_id
        existing = Payment.objects.filter(event_id=validated["event_id"]).first()
        if existing is not None:
            if existing.account_id != account.pk or existing.reported_by_id != user.pk or existing.submission_fingerprint != fingerprint:
                _error("This payment event ID cannot be reused with conflicting data.", "event_conflict", 409)
            return {"saved": True, "created": False, "payment": payment_payload(existing)}

        # Resolve optional related promise and activity
        promise = None
        if validated["promise_id"]:
            promise = PromiseToPay.objects.filter(pk=validated["promise_id"], activity__account_id=account.pk).first()
            if promise is None:
                _error("Referenced promise not found on this account.", "promise_not_found", 400)

        activity = None
        if validated["activity_id"]:
            activity = FieldActivity.objects.filter(pk=validated["activity_id"], account_id=account.pk, recorded_by_id=user.pk).first()
            if activity is None:
                _error("Referenced activity not found on this account.", "activity_not_found", 400)

        # Save private file
        relative_file_path = save_private_document(account.pk, file_obj, validated["doc_meta"])

        payment = Payment.objects.create(
            account=account,
            assignment=assignment,
            activity=activity,
            promise=promise,
            reported_by=user,
            event_id=validated["event_id"],
            amount=validated["amount"],
            paid_at=validated["paid_at"],
            method=validated["method"],
            reference=validated["reference"],
            notes=validated["notes"],
            status="reported",
            submission_fingerprint=fingerprint,
        )

        document = Document.objects.create(
            account=account,
            activity=activity,
            payment=payment,
            uploaded_by=user,
            kind="receipt",
            file=relative_file_path,
            original_name=validated["doc_meta"]["original_name"],
            mime_type=validated["doc_meta"]["mime_type"],
            size_bytes=validated["doc_meta"]["size_bytes"],
            sha256=validated["doc_meta"]["sha256"],
        )

        append_audit_event(
            actor=user,
            action="payment_reported",
            entity_type="Payment",
            entity_id=payment.pk,
            account=account,
            changes={"status": "reported", "amount": str(payment.amount), "method": payment.method},
        )
        append_audit_event(
            actor=user,
            action="document_uploaded",
            entity_type="Document",
            entity_id=document.pk,
            account=account,
            changes={"kind": "receipt", "size_bytes": document.size_bytes},
        )

        return {"saved": True, "created": True, "payment": payment_payload(payment, document)}


# ---------------------------------------------------------------------------
# Payment Verification Service (Back Office)
# ---------------------------------------------------------------------------

def can_verify_payments(user):
    return bool(user.is_authenticated and user.is_active and user.has_perm("collection.change_payment"))


def reviewable_payments(user):
    if not can_verify_payments(user):
        _error("Back Office payment verification permission is required.", "permission_required", 403)
    clients = ClientAccess.objects.filter(user_id=user.pk, is_active=True, client__is_active=True).values_list("client_id", flat=True)
    return Payment.objects.filter(
        account__client_id__in=clients
    ).exclude(reported_by_id=user.pk).select_related(
        "account__customer", "account__client", "reported_by", "verified_by", "assignment"
    ).prefetch_related("documents").order_by("paid_at", "pk")


def validate_payment_review(decision, reason):
    if not isinstance(decision, str) or decision not in {"verify", "reject"}:
        _error("Choose Verify payment or Reject payment.")
    return _text(reason, "Review reason", 5000, required=(decision == "reject"))


@_retry_transaction
def review_payment(user_id, payment_id, decision, reason):
    reason = validate_payment_review(decision, reason)
    user = get_user_model().objects.filter(pk=user_id).first()
    if user is None or not can_verify_payments(user):
        _error("Back Office payment verification permission is required.", "permission_required", 403)

    lookup = Payment.objects.filter(pk=payment_id).values("account_id").first()
    if lookup is None:
        _error("Payment record not found.", "payment_not_found", 404)

    with transaction.atomic():
        account = Account.objects.select_for_update(of=("self",)).get(pk=lookup["account_id"])
        client = Client.objects.select_for_update().get(pk=account.client_id)
        grant = ClientAccess.objects.select_for_update().filter(user_id=user.pk, client=client, is_active=True).first()
        if grant is None or not client.is_active:
            _error("Payment record not found.", "payment_not_found", 404)

        payment = Payment.objects.select_for_update().filter(pk=payment_id, account=account).first()
        if payment is None:
            _error("Payment record not found.", "payment_not_found", 404)

        if payment.reported_by_id == user.pk:
            _error("You cannot verify a payment you reported.", "self_review", 403)

        user = get_user_model().objects.filter(pk=user_id).first()
        if user is None or not can_verify_payments(user):
            _error("Back Office payment verification permission is required.", "permission_required", 403)

        target_status = "verified" if decision == "verify" else "rejected"

        if payment.status in {"verified", "rejected"}:
            if payment.status == target_status and payment.verified_by_id == user.pk and payment.rejection_reason == reason:
                return {"saved": True, "changed": False, "status": payment.status}
            _error("This payment has already been reviewed. Its decision cannot be overwritten.", "review_conflict", 409)

        if payment.status != "reported":
            _error("This payment record cannot be reviewed in its current state.", "invalid_state", 409)

        now = timezone.now()
        payment.status = target_status
        payment.verified_by = user
        payment.verified_at = max(now, payment.created_at)
        payment.rejection_reason = reason if target_status == "rejected" else ""
        payment.save(update_fields=["status", "verified_by", "verified_at", "rejection_reason", "updated_at"])

        audit_action = "payment_verified" if target_status == "verified" else "payment_rejected"
        append_audit_event(
            actor=user,
            action=audit_action,
            entity_type="Payment",
            entity_id=payment.pk,
            account=account,
            changes={"status": {"before": "reported", "after": target_status}},
        )

        return {"saved": True, "changed": True, "status": target_status}


# ---------------------------------------------------------------------------
# Private Document Access Control
# ---------------------------------------------------------------------------

def can_access_document(user, document):
    """Verify private access rules: Active assignment owner with client access, or authorized Back Office."""
    if not user.is_authenticated or not user.is_active:
        return False

    # Back Office with active ClientAccess
    if has_backoffice_access(user):
        return ClientAccess.objects.filter(
            user_id=user.pk,
            client_id=document.account.client_id,
            is_active=True,
            client__is_active=True,
        ).exists()

    # Ordinary collectors need current approved ownership AND active client access
    # for the account, including when they originally uploaded the document.
    profile = CollectorProfile.objects.filter(user=user, approval_status="approved").first()
    if profile is None:
        return False

    has_active_assignment = AccountAssignment.objects.filter(
        account_id=document.account_id,
        collector=profile,
        status="active",
    ).exists()
    if not has_active_assignment:
        return False

    return ClientAccess.objects.filter(
        user_id=user.pk,
        client_id=document.account.client_id,
        is_active=True,
        client__is_active=True,
    ).exists()


def stream_document_response(user, document_id):
    """Stream private document with security headers; returns 404 on access denial."""
    document = Document.objects.filter(pk=document_id).select_related("account__client").first()
    if document is None or not can_access_document(user, document):
        raise Http404("Document not found.")

    try:
        document_binary = default_storage.open(str(document.file), "rb")
    except (FileNotFoundError, OSError):
        raise Http404("Document binary not found.")

    response = FileResponse(document_binary, content_type=document.mime_type)
    safe_name = document.original_name.replace('"', '\\"')
    response["Content-Disposition"] = f'inline; filename="{safe_name}"'
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'"
    response["Cache-Control"] = "private, no-cache, no-store, must-revalidate"
    return response


# ---------------------------------------------------------------------------
# Dynamic Information Earning Rates, Commissions, and Payouts
# ---------------------------------------------------------------------------

def can_manage_earning_rules(user):
    return bool(user and user.is_authenticated and user.is_active and (user.is_superuser or user.has_perm("collection.manage_earning_rules")))


def can_review_commissions(user):
    return bool(user and user.is_authenticated and user.is_active and (user.is_superuser or user.has_perm("collection.review_commissions")))


def can_manage_payouts(user):
    return bool(user and user.is_authenticated and user.is_active and (user.is_superuser or user.has_perm("collection.manage_payouts")))


def can_manage_point_rules(user):
    return bool(user and user.is_authenticated and user.is_active and (user.is_superuser or user.has_perm("collection.manage_point_rules")))


def _check_client_access(user, client_id):
    if getattr(user, "is_superuser", False):
        return
    if not ClientAccess.objects.filter(user=user, client_id=client_id, is_active=True, client__is_active=True).exists():
        _error("This action is outside your permitted client scope.", "client_access_required", 403)


@_retry_transaction
def publish_earning_rule(user, *, client_id, category, amount):
    """Publish a new version of an information earning rate for a client/category.

    Retires the preceding active version instead of mutating historical data.
    """
    if not can_manage_earning_rules(user):
        _error("Permission to manage earning rules is required.", "permission_required", 403)

    client = Client.objects.filter(pk=client_id, is_active=True).first()
    if client is None:
        _error("Active client not found.", "client_not_found", 404)

    _check_client_access(user, client.pk)

    if category not in InformationCategory.values:
        _error("Select an approved information category.", "invalid_category", 400)

    try:
        val = Decimal(str(amount)).quantize(Decimal("0.01"))
        if val <= Decimal("0.00"):
            raise InvalidOperation
    except (InvalidOperation, TypeError, ValueError):
        _error("Provide a positive remuneration amount in PHP.", "invalid_amount", 400)

    now = timezone.now()

    with transaction.atomic():
        client = Client.objects.select_for_update().filter(pk=client_id, is_active=True).first()
        if client is None:
            _error("Active client not found.", "client_not_found", 404)
        existing = InformationEarningRule.objects.select_for_update().filter(client=client, category=category)
        max_version = 0
        active_rule = None
        for r in existing:
            if r.version > max_version:
                max_version = r.version
            if r.is_active:
                active_rule = r

        if active_rule is not None:
            active_rule.is_active = False
            active_rule.retired_at = now
            active_rule.retired_by = user
            active_rule.save(update_fields=["is_active", "retired_at", "retired_by", "updated_at"])

        next_version = max_version + 1
        new_rule = InformationEarningRule(
            client=client,
            category=category,
            amount=val,
            currency="PHP",
            version=next_version,
            is_active=True,
            created_by=user,
        )
        new_rule.full_clean()
        new_rule.save(force_insert=True)

        append_audit_event(
            actor=user,
            action="earning_rule_published",
            entity_type="InformationEarningRule",
            entity_id=new_rule.pk,
            changes={
                "category": new_rule.category,
                "version": new_rule.version,
                "amount": str(new_rule.amount),
            },
        )
        return new_rule


def create_commission_for_proposal(proposal, *, user=None, now=None):
    """Create a pending commission snapshot upon proposal approval if an active rule exists.

    If no active rule is published for the client and category, approval proceeds with no commission.
    """
    active_rule = InformationEarningRule.objects.filter(
        client_id=proposal.account.client_id,
        category=proposal.category,
        is_active=True,
    ).first()

    if active_rule is None:
        return None

    if Commission.objects.filter(proposal=proposal).exists():
        return Commission.objects.get(proposal=proposal)

    collector = proposal.assignment.collector
    commission = Commission(
        proposal=proposal,
        client=proposal.account.client,
        collector=collector,
        earning_rule=active_rule,
        category=proposal.category,
        amount=active_rule.amount,
        currency="PHP",
        rule_version=active_rule.version,
        status=Commission.Status.PENDING,
    )
    commission.full_clean()
    commission.save(force_insert=True)

    append_audit_event(
        actor=user,
        action="commission_created",
        entity_type="Commission",
        entity_id=commission.pk,
        account=proposal.account,
        changes={
            "category": commission.category,
            "amount": str(commission.amount),
            "status": commission.status,
        },
    )
    return commission


@_retry_transaction
def review_commission(user, commission_id, *, decision, reason=""):
    """Review a pending commission (approve or reject)."""
    if decision not in {"approve", "reject"}:
        _error("Choose approve or reject.", "invalid_decision", 400)

    if not isinstance(reason, str) or len(reason) > 5000 or (decision == "reject" and not reason.strip()):
        _error("Provide a rejection reason within 5000 characters.", "reason_required", 400)

    if not can_review_commissions(user):
        _error("Commission review permission is required.", "permission_required", 403)

    with transaction.atomic():
        commission = (
            Commission.objects.select_for_update()
            .select_related("proposal__account", "collector")
            .filter(pk=commission_id)
            .first()
        )
        if commission is None:
            _error("Commission record not found.", "not_found", 404)

        _check_client_access(user, commission.client_id)

        if commission.status != Commission.Status.PENDING:
            _error("This commission has already been reviewed.", "review_conflict", 409)

        now = timezone.now()
        target_status = Commission.Status.APPROVED if decision == "approve" else Commission.Status.REJECTED
        commission.status = target_status
        commission.reviewed_by = user
        commission.reviewed_at = now
        commission.rejection_reason = reason.strip() if decision == "reject" else ""
        commission.full_clean()
        commission.save(update_fields=["status", "reviewed_by", "reviewed_at", "rejection_reason", "updated_at"])

        audit_action = "commission_approved" if decision == "approve" else "commission_rejected"
        audit_changes = {"status": "approved"} if decision == "approve" else {"status": "rejected", "reason": reason.strip()[:100]}
        append_audit_event(
            actor=user,
            action=audit_action,
            entity_type="Commission",
            entity_id=commission.pk,
            account=commission.proposal.account,
            changes=audit_changes,
        )
        return commission


@_retry_transaction
def create_payout(user, *, client_id, collector_id, commission_ids=None):
    """Create a draft payout grouping approved commissions for one client and collector."""
    if not can_manage_payouts(user):
        _error("Permission to manage payouts is required.", "permission_required", 403)

    client = Client.objects.filter(pk=client_id, is_active=True).first()
    if client is None:
        _error("Active client not found.", "client_not_found", 404)

    _check_client_access(user, client.pk)

    collector = CollectorProfile.objects.filter(pk=collector_id, approval_status="approved").first()
    if collector is None:
        _error("Approved collector profile not found.", "collector_not_found", 404)

    with transaction.atomic():
        commissions_qs = (
            Commission.objects.select_for_update()
            .filter(
                client=client,
                collector=collector,
                status=Commission.Status.APPROVED,
                payout_item__isnull=True,
            )
        )
        if commission_ids:
            commissions_qs = commissions_qs.filter(pk__in=commission_ids)

        comm_list = list(commissions_qs)
        if not comm_list:
            _error("No approved unpaid commissions available for this payout.", "no_commissions", 400)

        total = sum(c.amount for c in comm_list)
        payout = Payout(
            client=client,
            collector=collector,
            currency="PHP",
            amount=total,
            status=Payout.Status.DRAFT,
            created_by=user,
        )
        payout.full_clean()
        payout.save(force_insert=True)

        for c in comm_list:
            item = PayoutItem(payout=payout, commission=c, amount=c.amount)
            item.full_clean()
            item.save(force_insert=True)

        append_audit_event(
            actor=user,
            action="payout_created",
            entity_type="Payout",
            entity_id=payout.pk,
            changes={
                "status": "draft",
                "item_count": len(comm_list),
                "amount": str(total),
            },
        )
        return payout


@_retry_transaction
def approve_payout(user, payout_id):
    """Approve a draft payout."""
    if not can_manage_payouts(user):
        _error("Permission to manage payouts is required.", "permission_required", 403)

    with transaction.atomic():
        payout = Payout.objects.select_for_update().filter(pk=payout_id).first()
        if payout is None:
            _error("Payout record not found.", "not_found", 404)

        _check_client_access(user, payout.client_id)

        if payout.status != Payout.Status.DRAFT:
            _error("Only draft payouts can be approved.", "invalid_state", 409)

        now = timezone.now()
        payout.status = Payout.Status.APPROVED
        payout.approved_by = user
        payout.approved_at = now
        payout.full_clean()
        payout.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])

        append_audit_event(
            actor=user,
            action="payout_approved",
            entity_type="Payout",
            entity_id=payout.pk,
            changes={"status": "approved"},
        )
        return payout


@_retry_transaction
def mark_payout_paid(user, payout_id, *, reference):
    """Mark an approved payout as paid and atomically update all linked commissions to paid."""
    if not can_manage_payouts(user):
        _error("Permission to manage payouts is required.", "permission_required", 403)

    if not isinstance(reference, str) or not reference.strip():
        _error("Provide an internal disbursement reference.", "reference_required", 400)

    clean_ref = reference.strip()
    if len(clean_ref) > 100:
        _error("Disbursement reference must not exceed 100 characters.", "reference_too_long", 400)

    with transaction.atomic():
        payout = Payout.objects.select_for_update().filter(pk=payout_id).first()
        if payout is None:
            _error("Payout record not found.", "not_found", 404)

        _check_client_access(user, payout.client_id)

        if payout.status != Payout.Status.APPROVED:
            _error("Only approved payouts can be marked as paid.", "invalid_state", 409)

        now = timezone.now()
        payout.status = Payout.Status.PAID
        payout.paid_by = user
        payout.paid_at = now
        payout.reference = clean_ref
        payout.full_clean()
        payout.save(update_fields=["status", "paid_by", "paid_at", "reference", "updated_at"])

        # Atomically mark all included commissions as paid
        comm_ids = list(payout.items.values_list("commission_id", flat=True))
        Commission.objects.filter(pk__in=comm_ids).update(
            status=Commission.Status.PAID,
            updated_at=now,
        )

        append_audit_event(
            actor=user,
            action="payout_paid",
            entity_type="Payout",
            entity_id=payout.pk,
            changes={"status": "paid", "reference": clean_ref[:100]},
        )
        return payout


def reviewable_earning_rules(user):
    """Return earning rules query for authorized Back Office clients."""
    if not can_manage_earning_rules(user):
        _error("Permission to manage earning rules is required.", "permission_required", 403)
    if getattr(user, "is_superuser", False):
        return InformationEarningRule.objects.select_related("client", "created_by", "retired_by").order_by("client__name", "category", "-version")
    client_ids = ClientAccess.objects.filter(user=user, is_active=True, client__is_active=True).values_list("client_id", flat=True)
    return InformationEarningRule.objects.filter(client_id__in=client_ids).select_related("client", "created_by", "retired_by").order_by("client__name", "category", "-version")


def reviewable_commissions(user):
    """Return commissions query for authorized Back Office clients."""
    if not can_review_commissions(user):
        _error("Permission to review commissions is required.", "permission_required", 403)
    if getattr(user, "is_superuser", False):
        return Commission.objects.select_related("client", "collector__user", "earning_rule", "proposal__account__customer", "reviewed_by").order_by("-created_at")
    client_ids = ClientAccess.objects.filter(user=user, is_active=True, client__is_active=True).values_list("client_id", flat=True)
    return Commission.objects.filter(client_id__in=client_ids).select_related("client", "collector__user", "earning_rule", "proposal__account__customer", "reviewed_by").order_by("-created_at")


def reviewable_payouts(user):
    """Return payouts query for authorized Back Office clients."""
    if not can_manage_payouts(user):
        _error("Permission to manage payouts is required.", "permission_required", 403)
    if getattr(user, "is_superuser", False):
        return Payout.objects.select_related("client", "collector__user", "created_by", "approved_by", "paid_by").prefetch_related("items__commission").order_by("-created_at")
    client_ids = ClientAccess.objects.filter(user=user, is_active=True, client__is_active=True).values_list("client_id", flat=True)
    return Payout.objects.filter(client_id__in=client_ids).select_related("client", "collector__user", "created_by", "approved_by", "paid_by").prefetch_related("items__commission").order_by("-created_at")


def collector_earnings_summary(collector):
    """Aggregate collector commissions across stages to avoid double-counting."""
    from django.db.models import Sum
    base = Commission.objects.filter(collector=collector)
    awaiting = base.filter(status=Commission.Status.PENDING).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
    approved_unpaid = base.filter(status=Commission.Status.APPROVED).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
    paid_out = base.filter(status=Commission.Status.PAID).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
    total = awaiting + approved_unpaid + paid_out
    return {
        "awaiting_approval": awaiting,
        "approved_unpaid": approved_unpaid,
        "paid_out": paid_out,
        "total": total,
    }


def collector_commissions_list(collector):
    return Commission.objects.filter(collector=collector).select_related("client", "earning_rule", "proposal__account").order_by("-created_at")


def collector_payouts_list(collector):
    return Payout.objects.filter(collector=collector).select_related("client").prefetch_related("items__commission").order_by("-created_at")


@_retry_transaction
def publish_point_rule(user, *, category, points=1):
    """Publish a new version of a point rule for an information category.

    Retires the preceding active version instead of mutating historical records.
    """
    if not can_manage_point_rules(user):
        _error("Permission to manage point rules is required.", "permission_required", 403)

    if category not in InformationCategory.values:
        _error("Select an approved information category.", "invalid_category", 400)

    try:
        pts = int(points)
        if pts != 1:
            raise ValueError
    except (TypeError, ValueError):
        _error("Current point rule version allows exactly 1 whole point.", "invalid_points", 400)

    now = timezone.now()

    with transaction.atomic():
        existing = PointRule.objects.select_for_update().filter(category=category)
        max_version = 0
        active_rule = None
        for r in existing:
            if r.version > max_version:
                max_version = r.version
            if r.is_active:
                active_rule = r

        if active_rule is not None:
            active_rule.is_active = False
            active_rule.retired_at = now
            active_rule.retired_by = user
            active_rule.save(update_fields=["is_active", "retired_at", "retired_by", "updated_at"])

        next_version = max_version + 1
        new_rule = PointRule(
            category=category,
            version=next_version,
            points=pts,
            is_active=True,
            created_by=user,
        )
        new_rule.full_clean()
        new_rule.save(force_insert=True)

        append_audit_event(
            actor=user,
            action="point_rule_published",
            entity_type="PointRule",
            entity_id=new_rule.pk,
            changes={
                "category": new_rule.category,
                "version": new_rule.version,
                "points": new_rule.points,
            },
        )
        return new_rule


def award_points_for_proposal(proposal, *, user=None, now=None):
    """Award verified points to the collector upon proposal approval if an active rule exists.

    If no active rule exists for the proposal category, approval succeeds with no points awarded.
    Idempotent: at most one PointTransaction is created per proposal.
    """
    from apps.accounts.information.models import AccountInformationProposal

    if getattr(proposal, "status", None) != AccountInformationProposal.Status.APPROVED:
        return None

    if proposal.category not in InformationCategory.values:
        return None

    active_rule = PointRule.objects.filter(
        category=proposal.category,
        is_active=True,
    ).first()

    if active_rule is None:
        return None

    if PointTransaction.objects.filter(proposal=proposal).exists():
        return PointTransaction.objects.get(proposal=proposal)

    collector = getattr(proposal.assignment, "collector", None)
    if collector is None:
        return None

    txn = PointTransaction(
        proposal=proposal,
        collector=collector,
        point_rule=active_rule,
        category=proposal.category,
        points=active_rule.points,
        rule_version=active_rule.version,
        approved_by=user,
        event_key=f"proposal:{proposal.pk}:{proposal.category}",
    )
    txn.full_clean()
    txn.save(force_insert=True)

    append_audit_event(
        actor=user,
        action="point_awarded",
        entity_type="PointTransaction",
        entity_id=txn.pk,
        account=proposal.account,
        changes={
            "category": txn.category,
            "points": txn.points,
        },
    )
    return txn


def reviewable_point_rules(user):
    """Return point rules query for authorized Back Office staff."""
    if not can_manage_point_rules(user):
        _error("Permission to manage point rules is required.", "permission_required", 403)
    return PointRule.objects.select_related("created_by", "retired_by").order_by("category", "-version")


def collector_points_summary(collector):
    """Aggregate collector verified points across all 5 categories."""
    from django.db.models import Sum
    txns = PointTransaction.objects.filter(collector=collector)
    total = txns.aggregate(s=Sum("points"))["s"] or 0
    breakdown = {}
    for code, label in InformationCategory.choices:
        pts = txns.filter(category=code).aggregate(s=Sum("points"))["s"] or 0
        breakdown[code] = {
            "code": code,
            "label": label,
            "points": pts,
        }
    return {
        "total": total,
        "breakdown": breakdown,
    }


def collector_point_transactions_list(collector):
    """Return point transactions query for a collector in reverse chronological order."""
    return PointTransaction.objects.filter(collector=collector).select_related(
        "point_rule", "proposal__account__customer", "approved_by"
    ).order_by("-created_at")


# ---------------------------------------------------------------------------
# Account-status side effects and status guards
# ---------------------------------------------------------------------------

def cancel_open_promises(account, reviewer, now):
    """Cancel open promises for a transitioned account and record each decision."""
    promises = list(
        PromiseToPay.objects.select_for_update().filter(
            activity__account_id=account.pk, status="open"
        ).order_by("pk")
    )
    for promise in promises:
        promise.status = "cancelled"
        promise.resolved_at = now
        promise.save(update_fields=["status", "resolved_at", "updated_at"])
        append_audit_event(
            actor=reviewer,
            action="promise_cancelled",
            entity_type="PromiseToPay",
            entity_id=promise.pk,
            account=account,
            changes={"status": {"before": "open", "after": "cancelled"}},
        )
    return len(promises)


def has_unverified_payments(account):
    """Return whether an account has a payment report awaiting Back Office review."""
    return Payment.objects.filter(
        account_id=getattr(account, "pk", account), status=Payment.Status.REPORTED
    ).exists()

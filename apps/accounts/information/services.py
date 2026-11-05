"""Transactional information proposals. No ownership or financial transitions."""

import copy
import time
import uuid
from functools import wraps

from django.contrib.auth import get_user_model
from django.contrib.gis.geos import Point
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.activities.models import Document
from apps.assignments.models import AccountAssignment
from apps.audit.services import append_audit_event
from apps.core.notification_services import notify_user
from apps.user.models import ClientAccess, CollectorProfile, has_backoffice_access

from .models import AccountInformationProposal as Proposal, CustomerContact
from .normalizers import normalize_address, normalize_email, normalize_phone, normalize_text, payload_fingerprint
from ..models import Account, Client, Customer, CustomerAddress


VALIDATION_PERMISSION = "accounts.validate_account_information"


class InformationWorkflowError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _retry_transaction(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        for attempt in range(3):
            try:
                return function(*args, **kwargs)
            except OperationalError as exc:
                if getattr(exc.__cause__, "sqlstate", None) not in {"40001", "40P01"}:
                    raise
                if attempt == 2:
                    raise InformationWorkflowError("busy", "Another update is in progress. Please retry.", 409) from exc
                time.sleep(0.05 * (attempt + 1))
    return wrapped


def _active_user(user):
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        raise InformationWorkflowError("authentication_required", "Please sign in with an active account.", 403)
    current = get_user_model().objects.filter(pk=user.pk, is_active=True).first()
    if current is None:
        raise InformationWorkflowError("authentication_required", "Please sign in with an active account.", 403)
    return current


def can_validate_information(user):
    return bool(getattr(user, "is_authenticated", False) and user.is_active and user.has_perm(VALIDATION_PERMISSION))


def _client_grant(user, client, lock=False):
    grants = ClientAccess.objects
    if lock:
        grants = grants.select_for_update()
    grant = grants.filter(user_id=user.pk, client_id=client.pk).first()
    if not client.is_active or grant is None or not grant.is_active:
        raise InformationWorkflowError("client_access_required", "This account is outside your permitted client scope.", 403)


def collector_assignment(user, assignment_id, lock=False):
    """Authorize reads and writes; callers requesting locks must be atomic.

    Lock order follows ownership transitions: account, profile, assignment,
    client, grant. Review subsequently locks customer and proposal.
    """
    user = _active_user(user)
    if has_backoffice_access(user) or can_validate_information(user):
        raise InformationWorkflowError("collector_required", "Use an ordinary collector account.", 403)
    lookup = AccountAssignment.objects.filter(pk=assignment_id).values("account_id", "collector_id").first()
    if lookup is None:
        raise InformationWorkflowError("not_found", "Assigned account not found.", 404)
    accounts, profiles, assignments, clients = Account.objects, CollectorProfile.objects, AccountAssignment.objects, Client.objects
    if lock:
        accounts = accounts.select_for_update(of=("self",))
        profiles = profiles.select_for_update(of=("self",))
        assignments = assignments.select_for_update(of=("self",))
        clients = clients.select_for_update()
    account = accounts.get(pk=lookup["account_id"])
    profile = profiles.select_related("user").get(pk=lookup["collector_id"])
    assignment = assignments.get(pk=assignment_id)
    client = clients.get(pk=account.client_id)
    if profile.user_id != user.pk or profile.approval_status != "approved" or not profile.user.is_active or assignment.status != "active" or assignment.collector_id != profile.pk:
        raise InformationWorkflowError("not_found", "Assigned account not found.", 404)
    _client_grant(user, client, lock)
    if account.status != "open":
        raise InformationWorkflowError("account_not_open", "Information updates require an open account.", 409)
    assignment.account, assignment.collector = account, profile
    return user, assignment


def reviewable_proposals(user):
    user = _active_user(user)
    if not can_validate_information(user):
        raise InformationWorkflowError("permission_required", "Information validation permission is required.", 403)
    clients = ClientAccess.objects.filter(user=user, is_active=True, client__is_active=True).values("client_id")
    return Proposal.objects.filter(account__client_id__in=clients).select_related("account__customer", "account__client", "account__collection_address", "assignment", "submitted_by", "reviewed_by", "evidence")


def information_queue(user, *, status="pending", category="all", query=""):
    proposals = reviewable_proposals(user)
    if status != "all":
        proposals = proposals.filter(status=status)
    if category != "all":
        proposals = proposals.filter(category=category)
    if query:
        proposals = proposals.filter(Q(account__reference__icontains=query) | Q(account__customer__display_name__icontains=query) | Q(submitted_by__username__icontains=query))
    return proposals.order_by("created_at", "pk")


def information_proposal_for_review(user, proposal_id):
    proposal = reviewable_proposals(user).filter(pk=proposal_id).first()
    if proposal is None:
        raise InformationWorkflowError("not_found", "Information proposal not found.", 404)
    return proposal


def information_backoffice_context(user):
    # Use the existing workflow capabilities without granting any additional authority.
    from apps.activities.services import can_review_visits, can_schedule_followups
    from apps.assignments.services import can_review_releases, can_manage_assignments
    from apps.collection.services import (
        can_verify_payments,
        can_manage_earning_rules,
        can_review_commissions,
        can_manage_payouts,
        can_manage_point_rules,
    )
    user = _active_user(user)
    if not can_validate_information(user):
        raise InformationWorkflowError("permission_required", "Information validation permission is required.", 403)
    return {
        "can_validate_information": True,
        "can_review": can_review_visits(user),
        "can_schedule": can_schedule_followups(user),
        "can_verify_payments": can_verify_payments(user),
        "can_review_releases": can_review_releases(user),
        "can_manage_assignments": can_manage_assignments(user),
        "can_manage_earning_rules": can_manage_earning_rules(user),
        "can_manage_point_rules": can_manage_point_rules(user),
        "can_review_commissions": can_review_commissions(user),
        "can_manage_payouts": can_manage_payouts(user),
        "backoffice_section": "information",
        "page_title": "Information Validation",
    }


def information_review_row(user, proposal):
    from apps.collection.services import can_access_document
    current = _snapshot(proposal.account, proposal.category)
    evidence = proposal.evidence
    return {"proposal": proposal, "current_values": _display_information(proposal.category, current),
        "submitted_values": _display_information(proposal.category, proposal.current_snapshot),
        "proposed_values": _display_information(proposal.category, proposal.proposed_payload),
        "stale": current != proposal.current_snapshot, "self_review": proposal.submitted_by_id == user.pk,
        "evidence_allowed": bool(evidence and evidence.account_id == proposal.account_id and can_access_document(user, evidence))}


def _display_information(category, payload):
    if category == "address":
        values = [payload["address_line"]] if payload.get("address_line") else []
        if payload.get("latitude") is not None and payload.get("longitude") is not None:
            values.append(f"Latitude {payload['latitude']}, longitude {payload['longitude']}")
        return values
    if category == "payment_arrangement":
        return [payload["description"]] if payload.get("description") else []
    if "contacts" in payload:
        return [contact["value"] for contact in payload["contacts"]]
    return [payload["value"]] if payload.get("value") else []


def collector_information_context(user, assignment_id):
    user, assignment = collector_assignment(user, assignment_id)
    return {
        "assignment": assignment,
        "collector_profile": assignment.collector,
        "collector_section": "accounts",
        "documents": Document.objects.filter(account_id=assignment.account_id).order_by("-created_at", "-pk"),
        "proposals": Proposal.objects.filter(assignment=assignment, submitted_by=user).select_related("evidence").order_by("-created_at", "-pk"),
    }


def _text(value, maximum=500):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise InformationWorkflowError("invalid_information", "Provide complete information within the allowed length.")
    return normalize_text(value)


def normalized_proposed_payload(category, payload):
    """Storage contract; category form validation will call this same boundary."""
    if not isinstance(payload, dict):
        raise InformationWorkflowError("invalid_information", "Provide structured proposed information.")
    if category in {"email", "phone", "contact_person"}:
        # TODO(decision): One value per proposal; multiple contacts and replacement policy remain unresolved.
        if set(payload) != {"value"}:
            raise InformationWorkflowError("invalid_information", "Provide one proposed contact value.")
        value = _text(payload["value"])
        if category == "email":
            value = normalize_email(value)
            try:
                validate_email(value)
            except ValidationError as exc:
                raise InformationWorkflowError("invalid_information", "Enter a valid email address.") from exc
        elif category == "phone":
            value = normalize_phone(value)
            if not value.lstrip("+").isascii() or not value.lstrip("+").isdigit() or "+" in value[1:]:
                raise InformationWorkflowError("invalid_information", "Enter a phone number using digits and an optional leading plus.")
        return {"value": value}
    if category == "address":
        if set(payload) != {"kind", "address_line", "latitude", "longitude"} or payload["kind"] not in CustomerAddress.Kind.values:
            raise InformationWorkflowError("invalid_information", "Provide the address type, address and coordinates.")
        result = {"kind": payload["kind"], "address_line": normalize_address(_text(payload["address_line"], 300)), "latitude": payload["latitude"], "longitude": payload["longitude"]}
        lat, lon = result["latitude"], result["longitude"]
        # TODO(decision): Text-only addresses are retained; activating a collection pin needs separate approval.
        if lat is not None or lon is not None:
            if isinstance(lat, bool) or isinstance(lon, bool) or not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
                raise InformationWorkflowError("invalid_information", "Provide valid latitude and longitude together.")
        return result
    if category == "payment_arrangement":
        return payment_arrangement_payload(payload)
    raise InformationWorkflowError("invalid_category", "Choose an information category.")


def payment_arrangement_payload(payload):
    # TODO(decision): Structured financial terms need approval; retain a description-only claim for now.
    if set(payload) != {"description"}:
        raise InformationWorkflowError("invalid_information", "Describe the proposed payment arrangement.")
    return {"description": _text(payload["description"], 5000)}


def _submission_data(assignment_id, category, proposed_payload, explanation, evidence_id, evidence_reference):
    if evidence_id is not None and (isinstance(evidence_id, bool) or not isinstance(evidence_id, int) or evidence_id <= 0):
        raise InformationWorkflowError("invalid_evidence", "Choose valid supporting evidence.")
    if not isinstance(evidence_reference, str) or len(evidence_reference) > 500:
        raise InformationWorkflowError("invalid_evidence", "Keep the evidence reference within 500 characters.")
    return {"assignment_id": assignment_id, "category": category,
            "proposed_payload": normalized_proposed_payload(category, copy.deepcopy(proposed_payload)),
            "explanation": _text(explanation, 5000), "evidence_id": evidence_id,
            "evidence_reference": normalize_text(evidence_reference)}


def _snapshot(account, category):
    if category == "address":
        address = account.collection_address
        return {} if address is None else {"id": address.pk, "kind": address.kind, "address_line": address.address_line,
            "latitude": address.position.y if address.position else None, "longitude": address.position.x if address.position else None}
    if category in {"email", "phone", "contact_person"}:
        return {"contacts": list(CustomerContact.objects.filter(customer_id=account.customer_id, kind=category, is_active=True).order_by("pk").values("id", "value", "normalized_value"))}
    return {}


def _audit(user, proposal, action):
    append_audit_event(actor=user, action=action, entity_type="AccountInformationProposal", entity_id=proposal.pk,
        account=proposal.account, request_id=proposal.event_id, changes={"category": proposal.category, "status": proposal.status})


@_retry_transaction
def submit_proposal(user, assignment_id, *, event_id, category, proposed_payload, explanation, evidence_id=None, evidence_reference=""):
    try:
        event_id = uuid.UUID(str(event_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise InformationWorkflowError("invalid_event_id", "Reload the form and try again.") from exc
    data = _submission_data(assignment_id, category, proposed_payload, explanation, evidence_id, evidence_reference)
    fingerprint = payload_fingerprint(data)
    with transaction.atomic():
        user, assignment = collector_assignment(user, assignment_id, lock=True)
        # Serialize changes across accounts belonging to the same customer.
        Customer.objects.select_for_update().get(pk=assignment.account.customer_id)
        existing = Proposal.objects.select_for_update().filter(event_id=event_id).first()
        if existing is not None:
            if existing.submitted_by_id == user.pk and existing.assignment_id == assignment.pk and existing.payload_fingerprint == fingerprint:
                return {"created": False, "proposal": existing}
            raise InformationWorkflowError("event_conflict", "This submission has changed. Reload the form before submitting again.", 409)
        # TODO(decision): Evidence is optional until category-specific evidence requirements are approved.
        evidence = None
        if evidence_id is not None:
            evidence = Document.objects.filter(pk=evidence_id, account_id=assignment.account_id).first()
            if evidence is None:
                raise InformationWorkflowError("invalid_evidence", "Supporting evidence was not found for this account.", 404)
        proposal = Proposal(account=assignment.account, customer_id=assignment.account.customer_id, assignment=assignment,
            submitted_by=user, category=category, current_snapshot=_snapshot(assignment.account, category),
            proposed_payload=data["proposed_payload"], explanation=data["explanation"], evidence=evidence,
            evidence_reference=data["evidence_reference"], event_id=event_id, payload_fingerprint=fingerprint)
        try:
            # Savepoint allows a global UUID collision on another account to become a clear conflict.
            with transaction.atomic():
                proposal.full_clean()
                proposal.save(force_insert=True)
        except (IntegrityError, ValidationError) as exc:
            if Proposal.objects.filter(event_id=event_id).exists():
                raise InformationWorkflowError("event_conflict", "This submission reference is already in use. Reload the form.", 409) from exc
            raise
        _audit(user, proposal, "information_proposal_submitted")
        return {"created": True, "proposal": proposal}


def _approval_effect(proposal, user, now):
    payload = normalized_proposed_payload(proposal.category, proposal.proposed_payload)
    if proposal.current_snapshot != _snapshot(proposal.account, proposal.category):
        raise InformationWorkflowError("stale_information", "Current information has changed. Reject this proposal and request an updated submission.", 409)
    if proposal.category == "address":
        # TODO(decision): Create a verified address without activating it as the collection address.
        address = CustomerAddress(customer_id=proposal.customer_id, kind=payload["kind"], address_line=payload["address_line"],
            position=None if payload["latitude"] is None else Point(payload["longitude"], payload["latitude"], srid=4326),
            verification_status="verified", added_by=proposal.submitted_by, verified_by=user, verified_at=now)
        address.full_clean()
        address.save(force_insert=True)
        proposal.approved_address = address
    elif proposal.category in {"email", "phone", "contact_person"}:
        # TODO(decision): Add rather than replace contacts; block equivalent active values pending deduplication policy.
        if CustomerContact.objects.filter(customer_id=proposal.customer_id, kind=proposal.category, normalized_value=payload["value"], is_active=True).exists():
            raise InformationWorkflowError("duplicate_information", "This information is already recorded for the customer.", 409)
        contact = CustomerContact(customer_id=proposal.customer_id, kind=proposal.category, value=payload["value"],
            normalized_value=payload["value"], validated_from_proposal=proposal, validated_by=user, validated_at=now)
        contact.full_clean()
        contact.save(force_insert=True)
    # Payment arrangement stays solely on the validated proposal; no financial side effects.


@_retry_transaction
def review_proposal(user, proposal_id, *, decision, reason=""):
    if decision not in {"approve", "reject"}:
        raise InformationWorkflowError("invalid_decision", "Choose approve or reject.")
    if not isinstance(reason, str) or len(reason) > 5000 or (decision == "reject" and not reason.strip()):
        raise InformationWorkflowError("reason_required", "Provide a rejection reason within 5000 characters.")
    user = _active_user(user)
    if not can_validate_information(user):
        raise InformationWorkflowError("permission_required", "Information validation permission is required.", 403)
    lookup = Proposal.objects.filter(pk=proposal_id).values("account_id", "assignment_id", "assignment__collector_id").first()
    if lookup is None:
        raise InformationWorkflowError("not_found", "Information proposal not found.", 404)
    with transaction.atomic():
        account = Account.objects.select_for_update(of=("self",)).get(pk=lookup["account_id"])
        CollectorProfile.objects.select_for_update(of=("self",)).get(pk=lookup["assignment__collector_id"])
        AccountAssignment.objects.select_for_update(of=("self",)).get(pk=lookup["assignment_id"])
        client = Client.objects.select_for_update().get(pk=account.client_id)
        _client_grant(user, client, lock=True)
        user = _active_user(user)
        if not can_validate_information(user):
            raise InformationWorkflowError("permission_required", "Information validation permission is required.", 403)
        Customer.objects.select_for_update().get(pk=account.customer_id)
        proposal = Proposal.objects.select_for_update(of=("self",)).get(pk=proposal_id)
        proposal.account = account
        if proposal.submitted_by_id == user.pk:
            raise InformationWorkflowError("self_review", "You cannot review your own submission.", 403)
        if proposal.status != "pending":
            raise InformationWorkflowError("review_conflict", "This proposal has already been reviewed.", 409)
        if proposal.customer_id != account.customer_id:
            raise InformationWorkflowError("stale_information", "The account's customer has changed. Request an updated submission.", 409)
        proposal.status = "approved" if decision == "approve" else "rejected"
        proposal.reviewed_by, proposal.reviewed_at, proposal.review_reason = user, timezone.now(), reason.strip()
        if decision == "approve":
            _approval_effect(proposal, user, proposal.reviewed_at)
        proposal.full_clean()
        proposal.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_reason", "approved_address", "updated_at"])
        _audit(user, proposal, "information_proposal_" + proposal.status)
        if decision == "approve":
            from apps.collection.services import award_points_for_proposal, create_commission_for_proposal
            create_commission_for_proposal(proposal, user=user, now=proposal.reviewed_at)
            award_points_for_proposal(proposal, user=user, now=proposal.reviewed_at)
        suffix = f" Reason: {proposal.review_reason}" if proposal.review_reason else ""
        notify_user(
            proposal.submitted_by_id,
            account,
            f"information-proposal-{proposal.pk}-{proposal.status}",
            f"Your {proposal.category.replace('_', ' ')} update for account {account.reference} was {proposal.status}.{suffix}",
        )
        return proposal

"""Collector GPS submission. Saving a fix does not grant offer eligibility."""

from dataclasses import dataclass
from datetime import datetime, timezone as datetime_timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING

from django.conf import settings
from django.contrib.gis.geos import Point
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.user.models import CollectorProfile, has_backoffice_access, BACK_OFFICE_PERMISSIONS

from .models import CollectorLocation


class LocationSubmissionError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class Observation:
    longitude: float
    latitude: float
    accuracy_m: Decimal
    captured_at: datetime


def submission_limits():
    limits = {}
    for key, setting in (
        ("max_age_seconds", "LOCATION_SUBMISSION_MAX_AGE_SECONDS"),
        ("max_accuracy_m", "LOCATION_SUBMISSION_MAX_ACCURACY_M"),
        ("max_clock_skew_seconds", "LOCATION_SUBMISSION_MAX_CLOCK_SKEW_SECONDS"),
    ):
        try:
            value = Decimal(str(getattr(settings, setting)))
        except (AttributeError, InvalidOperation):
            raise ImproperlyConfigured(f"{setting} must be a positive number.")
        if not value.is_finite() or value <= 0:
            raise ImproperlyConfigured(f"{setting} must be a positive number.")
        limits[key] = value
    if limits["max_accuracy_m"] > Decimal("999999.99"):
        raise ImproperlyConfigured("GPS submission accuracy exceeds storage capacity.")
    return limits


def _number(payload, field):
    value = payload[field]
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise LocationSubmissionError("invalid_observation", f"{field} must be a number.")
    value = Decimal(str(value))
    if not value.is_finite():
        raise LocationSubmissionError("invalid_observation", f"{field} must be finite.")
    return value


def validate_observation(payload):
    fields = {"longitude", "latitude", "accuracy_m", "captured_at"}
    if not isinstance(payload, dict) or set(payload) != fields:
        raise LocationSubmissionError(
            "invalid_observation", "Provide longitude, latitude, accuracy_m and captured_at only."
        )
    longitude = _number(payload, "longitude")
    latitude = _number(payload, "latitude")
    accuracy = _number(payload, "accuracy_m")
    if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
        raise LocationSubmissionError("invalid_observation", "Coordinates are outside valid bounds.")
    if accuracy < 0:
        raise LocationSubmissionError("invalid_observation", "Location accuracy cannot be negative.")
    if accuracy > submission_limits()["max_accuracy_m"]:
        raise LocationSubmissionError("inaccurate_location", "Location accuracy is too low. Please try again.")
    captured_at = payload["captured_at"]
    try:
        captured_at = parse_datetime(captured_at) if isinstance(captured_at, str) else None
    except (ValueError, OverflowError):
        captured_at = None
    if captured_at is None or timezone.is_naive(captured_at):
        raise LocationSubmissionError(
            "invalid_observation", "captured_at must be an ISO 8601 timestamp with a timezone."
        )
    try:
        captured_at = captured_at.astimezone(datetime_timezone.utc)
    except (ValueError, OverflowError):
        raise LocationSubmissionError("invalid_observation", "Observation time is outside valid bounds.")
    # Round uncertainty upwards so storage never makes accuracy appear better.
    observation = Observation(
        float(longitude), float(latitude),
        accuracy.quantize(Decimal("0.01"), rounding=ROUND_CEILING), captured_at,
    )
    _validate_age(observation, timezone.now())
    return observation


def _validate_age(observation, now):
    limits = submission_limits()
    age = Decimal(str((now - observation.captured_at).total_seconds()))
    if age > limits["max_age_seconds"]:
        raise LocationSubmissionError("stale_location", "Location is too old. Detect your location again.")
    if age < -limits["max_clock_skew_seconds"]:
        raise LocationSubmissionError("future_location", "Check your device clock and try again.")


def _result(location, changed):
    now = timezone.now()
    age = max(0, (now - location.captured_at).total_seconds())
    limits = submission_limits()
    return {
        "saved": True,
        "changed": changed,
        "location": {
            "longitude": location.position.x,
            "latitude": location.position.y,
            "accuracy_m": float(location.accuracy_m),
            "captured_at": location.captured_at.isoformat(),
            "received_at": location.received_at.isoformat(),
        },
        "server_time": now.isoformat(),
        "age_seconds": round(age, 3),
        "fresh_for_submission": age <= limits["max_age_seconds"],
        "matching_eligibility": "not_evaluated",
    }


def submit_collector_location(user_id, payload):
    observation = validate_observation(payload)
    with transaction.atomic():
        # Lock the parent even when no location row exists yet. Other location
        # writers/cleanup must use the same profile-before-location discipline.
        profile = (
            CollectorProfile.objects.select_related("user")
            .select_for_update(of=("self",))
            .filter(user_id=user_id).first()
        )
        if profile is None or not profile.user.is_active:
            raise LocationSubmissionError("collector_required", "An active collector account is required.", 403)
        if has_backoffice_access(profile.user):
            raise LocationSubmissionError("collector_portal_forbidden", "Use the Back Office portal with this account.", 403)
        if profile.approval_status != CollectorProfile.ApprovalStatus.APPROVED:
            raise LocationSubmissionError("approval_required", "Collector approval is required.", 403)
        # Recheck after lock waits; receipt time must never refresh an old fix.
        _validate_age(observation, timezone.now())
        location = CollectorLocation.objects.select_for_update().filter(collector=profile).first()
        if location is not None:
            if observation.captured_at < location.captured_at:
                raise LocationSubmissionError("older_observation", "A newer location is already saved.", 409)
            if observation.captured_at == location.captured_at:
                if (
                    location.position.x == observation.longitude
                    and location.position.y == observation.latitude
                    and location.accuracy_m == observation.accuracy_m
                ):
                    return _result(location, changed=False)
                raise LocationSubmissionError("conflicting_observation", "Observation time conflicts with a saved location.", 409)
            location.position = Point(observation.longitude, observation.latitude, srid=4326)
            location.accuracy_m = observation.accuracy_m
            location.captured_at = observation.captured_at
            location.save(update_fields=["position", "accuracy_m", "captured_at", "received_at", "updated_at"])
        else:
            location = CollectorLocation.objects.create(
                collector=profile,
                position=Point(observation.longitude, observation.latitude, srid=4326),
                accuracy_m=observation.accuracy_m,
                captured_at=observation.captured_at,
            )
        # Explicit manual submission is allowed while offline. This does not
        # change availability or make the collector eligible for new offers.
        return _result(location, changed=True)


# Offer/assignment workflows: Account -> CollectorProfile -> AccountOffer,
# then location, client, target address and earning-rule rows.
from datetime import timedelta
from functools import wraps
import math
import time
from urllib.parse import urlencode

from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.measure import D
from django.core.exceptions import ValidationError
from django.db import OperationalError
from django.db.models import Count, F, Prefetch, Q
from django.urls import reverse

from apps.accounts.guards import check_account_is_open
from apps.accounts.models import Account, Client, CustomerAddress
from apps.accounts.services import validate_account_for_offers
from apps.audit.services import append_audit_event
from apps.collection.models import CommissionRule
from apps.core.models import Notification
import uuid
from apps.user.models import ClientAccess
from .models import AccountOffer, AccountAssignment, AssignmentReleaseRequest


class OfferWorkflowError(LocationSubmissionError):
    pass


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
                    raise OfferWorkflowError("busy", "Please retry; another update is in progress.", 409) from exc
                time.sleep(0.05 * (attempt + 1))
    return wrapped


def collector_profile(user_id, lock=False):
    queryset = CollectorProfile.objects.select_related("user")
    if lock:
        queryset = queryset.select_for_update(of=("self",))
    profile = queryset.filter(user_id=user_id).first()
    if profile is None or not profile.user.is_active:
        raise OfferWorkflowError("collector_required", "An active collector account is required.", 403)
    if has_backoffice_access(profile.user):
        raise OfferWorkflowError("collector_portal_forbidden", "Use the Back Office portal with this account.", 403)
    if profile.approval_status != "approved":
        raise OfferWorkflowError("approval_required", "Collector approval is required.", 403)
    return profile


def client_ids(profile):
    # Explicit grants are used for this initial implementation. No implicit
    # staff/superuser collector scope; a grant is not finance authority.
    return ClientAccess.objects.filter(user_id=profile.user_id, is_active=True, client__is_active=True).values_list("client_id", flat=True)


ROLLING_ACCEPTANCE_LIMIT = 10
ROLLING_ACCEPTANCE_HOURS = 24


def recent_acceptances(profile, now):
    """Count accepted offer assignments across all clients and ownership statuses."""
    return profile.account_assignments.filter(offer__isnull=False,
        created_at__gt=now-timedelta(hours=ROLLING_ACCEPTANCE_HOURS),created_at__lte=now)


def acceptance_allowance(profile, now=None):
    now=timezone.now() if now is None else now
    history=recent_acceptances(profile,now)
    used=history.count()
    next_time=None
    if used>=ROLLING_ACCEPTANCE_LIMIT:
        # Historical excess needs enough rows to expire to leave one free slot.
        stamp=history.order_by("created_at","pk").values_list("created_at",flat=True)[used-ROLLING_ACCEPTANCE_LIMIT]
        next_time=stamp+timedelta(hours=ROLLING_ACCEPTANCE_HOURS)
    return {"used":used,"limit":ROLLING_ACCEPTANCE_LIMIT,"remaining":max(0,ROLLING_ACCEPTANCE_LIMIT-used),
        "next_available_at":next_time.isoformat() if next_time else None}


def active_workload(profile):
    return profile.account_assignments.filter(status="active").count()


def _location(profile, lock=False):
    queryset = CollectorLocation.objects
    if lock:
        queryset = queryset.select_for_update()
    return queryset.filter(collector=profile).first()


def location_valid(location, client, now):
    if location is None:
        return False
    age = (now - location.captured_at).total_seconds()
    return (-client.gps_max_clock_skew_seconds <= age <= client.gps_max_age_seconds
            and location.accuracy_m <= client.gps_max_accuracy_m)


def _eligible(profile, client, location, now):
    if profile.approval_status != "approved" or not profile.user.is_active or has_backoffice_access(profile.user):
        raise OfferWorkflowError("approval_required", "An approved active collector is required.", 403)
    if profile.availability != "online":
        raise OfferWorkflowError("offline", "Go online to receive or accept offers.", 409)
    if recent_acceptances(profile,now).count() >= ROLLING_ACCEPTANCE_LIMIT:
        raise OfferWorkflowError("daily_limit_reached", "You have accepted 10 accounts in the last 24 hours. Try again when an earlier acceptance leaves that window.", 409)
    if not client.is_active or not ClientAccess.objects.filter(user_id=profile.user_id, client=client, is_active=True).exists():
        raise OfferWorkflowError("client_access_required", "This account is outside your permitted client scope.", 403)
    if client.require_service_area_membership:
        raise OfferWorkflowError("service_area_required", "Service-area matching needs supervisor configuration.", 409)
    if not location_valid(location, client, now):
        raise OfferWorkflowError("location_required", "Update your location before requesting or accepting offers.", 409)


def _policy(client):
    return {
        "policy_version": client.policy_version,
        "gps_max_age_seconds": client.gps_max_age_seconds,
        "gps_max_accuracy_m": str(client.gps_max_accuracy_m),
        "gps_max_clock_skew_seconds": client.gps_max_clock_skew_seconds,
        "matching_strategy": client.matching_strategy,
        "max_offer_recipients": client.max_offer_recipients,
        "require_service_area_membership": client.require_service_area_membership,
        "first_action_hours": client.first_action_hours,
        "offer_timeout_minutes": client.offer_timeout_minutes,
        "allow_collector_release": client.allow_collector_release,
        "auto_reassignment_enabled": client.auto_reassignment_enabled,
    }


def _terms(rule):
    bands = [{"lower": str(t.lower_amount), "upper": None if t.upper_amount is None else str(t.upper_amount), "percentage": str(t.percentage)} for t in rule.tiers.order_by("lower_amount")]
    return {
        "code": rule.code, "version": rule.version, "name": rule.name,
        "kind": rule.kind, "percentage": None if rule.percentage is None else str(rule.percentage),
        "fixed_amount": None if rule.fixed_amount is None else str(rule.fixed_amount),
        "currency": rule.currency, "earning_basis": rule.earning_basis,
        "fixed_frequency": rule.fixed_frequency, "tier_mode": rule.tier_mode, "tiers": bands,
    }


def terms_display(terms):
    basis = "verified payment" if terms["earning_basis"] == "verified_payment" else "verified account resolution"
    if terms["kind"] == "percentage":
        return f"{Decimal(terms['percentage']).normalize():f}% of {basis}"
    if terms["kind"] == "fixed":
        frequency = "once per account" if terms["fixed_frequency"] == "once_per_account" else "per payment"
        return f"{terms['currency']} {Decimal(terms['fixed_amount']):,.2f}, {frequency}; {basis}"
    mode = "whole amount" if terms["tier_mode"] == "whole_amount" else "marginal bands"
    rates = "; ".join(f"{b['lower']}–{b['upper'] or 'above'}: {Decimal(b['percentage']).normalize():f}%" for b in terms["tiers"])
    return f"Tiered commission ({mode}), {basis}. {rates}"


def approximate_area(point):
    # Fixed grid centre rather than the customer's exact coordinate. Repeated
    # calls disclose the same area, not random points which can be averaged.
    return {"latitude": round(min(89.995, max(-89.995, math.floor(point.y / 0.01) * 0.01 + 0.005)), 3),
            "longitude": round((math.floor(point.x / 0.01) * 0.01 + 0.005 + 180) % 360 - 180, 3),
            "radius_m": 900}


def _distance(profile, location, target):
    value = CollectorLocation.objects.filter(pk=location.pk).annotate(target_distance=Distance("position", target)).values_list("target_distance", flat=True).get()
    metres = value.m
    if metres > profile.matching_radius_m:
        raise OfferWorkflowError("outside_radius", "The collection area is outside your current radius.", 409)
    return Decimal(str(metres)).quantize(Decimal("0.01"))


@_retry_transaction
def change_availability(user_id, availability):
    if availability not in {"online", "offline"}:
        raise OfferWorkflowError("invalid_availability", "Choose online or offline.")
    with transaction.atomic():
        profile = collector_profile(user_id, lock=True)
        previous = profile.availability
        if previous != availability:
            profile.availability = availability
            profile.save(update_fields=["availability", "updated_at"])
            append_audit_event(actor=profile.user, action="collector_availability_changed", entity_type="CollectorProfile", entity_id=profile.pk,
                               changes={"availability": {"before": previous, "after": availability}})
        return {"availability": profile.availability, "workload": active_workload(profile), "daily_acceptance": acceptance_allowance(profile)}


def _offer_payload(offer):
    address = offer.target_address
    area = f"{address.territory.name}, {address.territory.city}" if address.territory_id else "Nearby collection area"
    return {
        "id": offer.pk, "reference": offer.account.reference, "area": area,
        "approximate_area": approximate_area(offer.target_position),
        "distance_m": int(round(float(offer.distance_m) / 100) * 100),
        "task_summary": "Collection visit", "commission_terms": terms_display(offer.matching_snapshot["commission_terms"]),
        "expires_at": offer.expires_at.isoformat(),
        "accept_url": reverse("assignments:accept_offer", args=[offer.pk]),
        "decline_url": reverse("assignments:decline_offer", args=[offer.pk]),
    }


def offers_state(user_id):
    profile = collector_profile(user_id)
    now = timezone.now()
    location = _location(profile)
    clients = list(Client.objects.filter(pk__in=client_ids(profile)))
    workload = active_workload(profile)
    allowance=acceptance_allowance(profile,now)
    state = "ready"
    if profile.availability != "online":
        state = "offline"
    elif allowance["remaining"]==0:
        state = "daily_limit_reached"
    elif not clients:
        state = "no_client_access"
    elif not any(location_valid(location, client, now) for client in clients):
        state = "location_required"
    offers = []
    if state == "ready":
        queryset = AccountOffer.objects.filter(collector=profile, status="pending", expires_at__gt=now, account__source_system__in=settings.COLLECTION_OFFER_SOURCE_SYSTEMS, account__client_id__in=[c.pk for c in clients], account__status="open", account__collection_address__verification_status="verified", account__commission_rule__is_active=True).exclude(account__assignments__status="active").select_related("account", "account__client", "account__collection_address", "target_address__territory", "commission_rule").order_by("distance_m", "pk")
        for offer in queryset[:60]:
            client = offer.account.client
            if not location_valid(location, client, now) or client.require_service_area_membership:
                continue
            if (offer.matching_snapshot.get("policy") != _policy(client)
                or offer.account.collection_address_id != offer.target_address_id
                or offer.account.commission_rule_id != offer.commission_rule_id
                or offer.matching_snapshot.get("commission_terms") != _terms(offer.commission_rule)
                or offer.account.collection_address.position is None
                or offer.account.collection_address.position.x != offer.target_position.x
                or offer.account.collection_address.position.y != offer.target_position.y):
                continue
            offers.append(_offer_payload(offer))
    return {
        "state": state, "availability": profile.availability, "daily_acceptance": allowance,
        "workload": workload, "radius_m": profile.matching_radius_m, "server_time": now.isoformat(),
        "location": None if location is None else {"latitude": location.position.y, "longitude": location.position.x,
                                                   "accuracy_m": float(location.accuracy_m), "captured_at": location.captured_at.isoformat()},
        "offers": offers,
    }


def _candidate_profiles(account, client, now):
    from django.contrib.auth import get_user_model
    from django.contrib.auth.models import Permission
    permission_filter=Q(pk__in=[])
    for permission_name in BACK_OFFICE_PERMISSIONS:
        app_label,codename=permission_name.split(".",1)
        permission_filter |= Q(content_type__app_label=app_label,codename=codename)
    permissions=Permission.objects.filter(permission_filter)
    backoffice=get_user_model().objects.filter(Q(is_superuser=True)|Q(user_permissions__in=permissions)|Q(groups__permissions__in=permissions)).values_list("pk",flat=True)
    declined = AccountOffer.objects.filter(account=account, status="declined", expires_at__gt=now).values_list("collector_id", flat=True)
    return list(CollectorProfile.objects.filter(
        approval_status="approved", availability="online", user__is_active=True,
        user__client_accesses__client=client, user__client_accesses__is_active=True,
        location__captured_at__gte=now - timedelta(seconds=client.gps_max_age_seconds),
        location__captured_at__lte=now + timedelta(seconds=client.gps_max_clock_skew_seconds),
        location__accuracy_m__lte=client.gps_max_accuracy_m,
    ).exclude(pk__in=declined).exclude(user_id__in=backoffice).annotate(
        accepted_recent=Count("account_assignments", filter=Q(account_assignments__offer__isnull=False,account_assignments__created_at__gt=now-timedelta(hours=ROLLING_ACCEPTANCE_HOURS),account_assignments__created_at__lte=now), distinct=True),
        target_distance=Distance("location__position", account.collection_address.position),
    ).filter(accepted_recent__lt=ROLLING_ACCEPTANCE_LIMIT, target_distance__lte=F("matching_radius_m")).order_by("target_distance", "pk").values_list("pk", flat=True)[:client.max_offer_recipients if client.matching_strategy == "fan_out" else 1])


@_retry_transaction
def _match_account(account_id):
    with transaction.atomic():
        account = Account.objects.select_for_update(of=("self",)).select_related("client", "customer", "collection_address", "commission_rule").get(pk=account_id)
        now = timezone.now()
        if account.source_system not in settings.COLLECTION_OFFER_SOURCE_SYSTEMS or account.assignments.filter(status="active").exists() or account.status != "open":
            return
        client = account.client
        if client.matching_strategy == "manual" or client.require_service_area_membership or not client.is_active:
            return
        try:
            validate_account_for_offers(account)
        except ValidationError:
            return
        candidate_ids = _candidate_profiles(account, client, now)
        profiles = list(CollectorProfile.objects.select_for_update(of=("self",)).select_related("user").filter(pk__in=candidate_ids).order_by("pk"))
        # Lock every affected offer in PK order after profile locks.
        pending = list(account.offers.select_for_update(of=("self",)).select_related("collector").filter(status="pending").order_by("pk"))
        Client.objects.select_for_update().get(pk=client.pk)
        address = CustomerAddress.objects.select_for_update().get(pk=account.collection_address_id)
        rule = CommissionRule.objects.select_for_update().get(pk=account.commission_rule_id)
        account.client = client = Client.objects.get(pk=client.pk)
        account.collection_address = address
        account.commission_rule = rule
        try:
            validate_account_for_offers(account)
        except ValidationError:
            return
        if client.matching_strategy == "manual" or client.require_service_area_membership or not client.is_active:
            return
        policy, terms = _policy(client), _terms(rule)
        for offer in pending:
            changed = (offer.target_address_id != address.pk or offer.target_position.x != address.position.x
                       or offer.target_position.y != address.position.y or offer.commission_rule_id != rule.pk
                       or offer.matching_snapshot.get("policy") != policy or offer.matching_snapshot.get("commission_terms") != terms)
            exhausted=recent_acceptances(offer.collector,now).count()>=ROLLING_ACCEPTANCE_LIMIT
            if offer.expires_at <= now or changed or exhausted:
                offer.status = "expired" if offer.expires_at <= now else "withdrawn"
                offer.save(update_fields=["status", "updated_at"])
        existing = list(account.offers.filter(status="pending").values_list("collector_id", flat=True))
        limit = client.max_offer_recipients if client.matching_strategy == "fan_out" else 1
        for profile in profiles:
            if profile.pk in existing or len(existing) >= limit:
                continue
            location = _location(profile, lock=True)
            try:
                _eligible(profile, client, location, timezone.now())
                distance = _distance(profile, location, address.position)
            except OfferWorkflowError:
                continue
            snapshot = {"policy": policy, "commission_terms": terms, "radius_m": profile.matching_radius_m,
                        "acceptance_limit":ROLLING_ACCEPTANCE_LIMIT,"acceptance_window_hours":ROLLING_ACCEPTANCE_HOURS, "observation_time": location.captured_at.isoformat()}
            offer = AccountOffer.objects.create(account=account, collector=profile, target_address=address,
                target_position=address.position.clone(), commission_rule=rule, expires_at=timezone.now() + timedelta(minutes=client.offer_timeout_minutes),
                collector_position=location.position.clone(), location_captured_at=location.captured_at, distance_m=distance, matching_snapshot=snapshot)
            Notification.objects.create(recipient=profile.user, kind="offer", account=account, offer=offer,
                message="An account offer is available nearby.", dedup_key=f"offer:{offer.pk}:user:{profile.user_id}")
            append_audit_event(actor=None, action="offer_created", entity_type="AccountOffer", entity_id=offer.pk, account=account, changes={"status": "pending"})
            existing.append(profile.pk)


def refresh_offers(user_id):
    profile = collector_profile(user_id)
    state = offers_state(user_id)
    if state["state"] != "ready":
        return state
    location = _location(profile)
    candidates = Account.objects.filter(client_id__in=client_ids(profile), source_system__in=settings.COLLECTION_OFFER_SOURCE_SYSTEMS, status="open",
        collection_address__verification_status="verified", collection_address__position__isnull=False,
        commission_rule__is_active=True, collection_address__position__dwithin=(location.position, D(m=profile.matching_radius_m)),
    ).exclude(assignments__status="active").annotate(distance=Distance("collection_address__position", location.position)).order_by("distance", "pk").values_list("pk", flat=True)[:40]
    for account_id in list(candidates):
        _match_account(account_id)
    return offers_state(user_id)


def assignment_payload(assignment):
    policy_snapshot = assignment.policy_snapshot or {}
    commission_terms = policy_snapshot.get("commission_terms")
    if commission_terms is None:
        # Older assignments may predate frozen commission terms in the snapshot.
        # The protected commission_rule relation is the best available source
        # for those records and keeps the detail page readable.
        commission_terms = _terms(assignment.commission_rule)
    return {
        "reference": assignment.account.reference, "customer_name": assignment.account.customer.display_name,
        "address": assignment.target_address.address_line, "status": assignment.status,
        "latitude": assignment.target_position.y, "longitude": assignment.target_position.x,
        "first_action_due_at": assignment.first_action_due_at.isoformat(),
        "commission_terms": terms_display(commission_terms),
        "detail_url": reverse("assignments:account_detail", args=[assignment.pk]),
        "directions_url": "https://www.google.com/maps/dir/?" + urlencode({"api": 1, "destination": f"{assignment.target_position.y},{assignment.target_position.x}", "dir_action": "navigate"}),
    }


@_retry_transaction
def respond_to_offer(user_id, offer_id, action):
    if action not in {"accept", "decline"}:
        raise OfferWorkflowError("invalid_action", "Choose accept or decline.")
    lookup = AccountOffer.objects.filter(pk=offer_id, collector__user_id=user_id).values("account_id").first()
    if lookup is None:
        raise OfferWorkflowError("offer_not_found", "Offer not found.", 404)
    with transaction.atomic():
        account = Account.objects.select_for_update(of=("self",)).select_related("customer").get(pk=lookup["account_id"])
        profile = collector_profile(user_id, lock=True)
        all_pending_ids = list(account.offers.filter(Q(status="pending") | Q(pk=offer_id)).order_by("pk").values_list("pk", flat=True))
        offers = list(AccountOffer.objects.select_for_update().filter(pk__in=all_pending_ids).order_by("pk"))
        offer = next((value for value in offers if value.pk == offer_id and value.collector_id == profile.pk), None)
        if offer is None:
            raise OfferWorkflowError("offer_not_found", "Offer not found.", 404)
        if offer.status == "accepted" and action == "accept":
            assignment = AccountAssignment.objects.select_related("account__customer", "target_address").filter(offer=offer, collector=profile, status="active").first()
            if assignment and assignment.account.client_id in client_ids(profile):
                return {"accepted": True, "assignment": assignment_payload(assignment), "workload": active_workload(profile), "daily_acceptance": acceptance_allowance(profile)}
        if offer.status == "declined" and action == "decline":
            return {"declined": True}
        check_account_is_open(account, OfferWorkflowError)
        if action == "accept" and account.source_system not in settings.COLLECTION_OFFER_SOURCE_SYSTEMS:
            raise OfferWorkflowError("workflow_not_enabled", "This account is not enabled for acceptance yet.", 409)
        if offer.status != "pending" or offer.expires_at <= timezone.now():
            raise OfferWorkflowError("offer_unavailable", "This offer has expired or is no longer available.", 409)
        if account.client_id not in client_ids(profile):
            raise OfferWorkflowError("client_access_required", "This account is outside your permitted client scope.", 403)
        if action == "decline":
            offer.status = "declined"
            offer.responded_at = timezone.now()
            offer.save(update_fields=["status", "responded_at", "updated_at"])
            append_audit_event(actor=profile.user, action="offer_declined", entity_type="AccountOffer", entity_id=offer.pk, account=account, changes={"status": {"before": "pending", "after": "declined"}})
            return {"declined": True}
        location = _location(profile, lock=True)
        client = Client.objects.select_for_update().get(pk=account.client_id)
        address = CustomerAddress.objects.select_for_update().get(pk=offer.target_address_id)
        rule = CommissionRule.objects.select_for_update().get(pk=offer.commission_rule_id)
        account.client, account.collection_address, account.commission_rule = client, address, rule
        # Check persisted target links before substituting locked related objects.
        links = Account.objects.filter(pk=account.pk).values("collection_address_id", "commission_rule_id").get()
        if links["collection_address_id"] != offer.target_address_id or links["commission_rule_id"] != offer.commission_rule_id:
            raise OfferWorkflowError("terms_changed", "This offer changed. Refresh your offers.", 409)
        try:
            validate_account_for_offers(account)
        except ValidationError:
            raise OfferWorkflowError("offer_unavailable", "This account is no longer available.", 409)
        now = timezone.now()
        _eligible(profile, client, location, now)
        if (offer.matching_snapshot.get("policy") != _policy(client)
            or offer.matching_snapshot.get("commission_terms") != _terms(rule)
            or offer.target_position.x != address.position.x or offer.target_position.y != address.position.y):
            raise OfferWorkflowError("terms_changed", "This offer changed. Refresh your offers.", 409)
        _distance(profile, location, offer.target_position)
        if account.assignments.filter(status="active").exists():
            raise OfferWorkflowError("already_assigned", "This account has already been assigned.", 409)
        if offer.expires_at <= now:
            raise OfferWorkflowError("offer_unavailable", "This offer expired. Refresh your offers.", 409)
        policy = dict(offer.matching_snapshot["policy"])
        policy["commission_terms"] = offer.matching_snapshot["commission_terms"]
        policy["acceptance_limit"]=ROLLING_ACCEPTANCE_LIMIT
        policy["acceptance_window_hours"]=ROLLING_ACCEPTANCE_HOURS
        assignment = AccountAssignment.objects.create(account=account, collector=profile, offer=offer, target_address=address,
            commission_rule=rule, target_position=offer.target_position.clone(), policy_snapshot=policy,
            first_action_due_at=now + timedelta(hours=policy["first_action_hours"]))
        offer.status, offer.responded_at = "accepted", now
        offer.save(update_fields=["status", "responded_at", "updated_at"])
        for other in offers:
            if other.pk != offer.pk and other.status == "pending":
                other.status = "withdrawn"
                other.save(update_fields=["status", "updated_at"])
        append_audit_event(actor=profile.user, action="offer_accepted", entity_type="AccountOffer", entity_id=offer.pk, account=account,
                           changes={"status": {"before": "pending", "after": "accepted"}})
        Notification.objects.get_or_create(dedup_key=f"assignment:{assignment.pk}:user:{profile.user_id}", defaults={"recipient": profile.user,
            "kind": "system", "account": account, "offer": offer, "message": "An account is now assigned to you."})
        return {"accepted": True, "assignment": assignment_payload(assignment), "workload": active_workload(profile), "daily_acceptance": acceptance_allowance(profile)}


def can_review_releases(user):
    if not user or not user.is_authenticated or not user.is_active:
        return False
    if getattr(user, "is_superuser", False):
        return True
    for attr in ("_perm_cache", "_user_perm_cache", "_group_perm_cache"):
        if hasattr(user, attr):
            try:
                delattr(user, attr)
            except (AttributeError, KeyError):
                pass
    has_perm = getattr(user, "has_perm", None)
    return bool(has_perm and has_perm("assignments.review_release_requests"))


def reviewable_release_requests(user):
    if not can_review_releases(user):
        raise OfferWorkflowError("permission_required", "Back Office release review permission is required.", 403)
    clients = ClientAccess.objects.filter(user_id=user.pk, is_active=True, client__is_active=True).values_list("client_id", flat=True)
    return AssignmentReleaseRequest.objects.filter(
        assignment__account__client_id__in=clients,
    ).exclude(requested_by_id=user.pk).select_related(
        "assignment__account__customer",
        "assignment__account__client",
        "assignment__target_address",
        "assignment__collector__user",
        "requested_by",
        "reviewed_by",
    ).prefetch_related(
        "assignment__activities",
    ).order_by("created_at", "pk")


def validate_release_request_payload(payload):
    if not isinstance(payload, dict):
        raise OfferWorkflowError("invalid_payload", "Submit a release request form.")
    event_id = payload.get("event_id")
    if not event_id or not isinstance(event_id, str):
        raise OfferWorkflowError("invalid_event_id", "A valid event_id is required.")
    try:
        parsed_uuid = uuid.UUID(str(event_id))
    except (ValueError, AttributeError, TypeError):
        raise OfferWorkflowError("invalid_event_id", "A valid UUID event_id is required.")
    reason = payload.get("reason")
    if not isinstance(reason, str):
        raise OfferWorkflowError("invalid_reason", "A reason is required.")
    reason = reason.strip()
    if not reason:
        raise OfferWorkflowError("reason_required", "A reason is required to request release.")
    if len(reason) > 5000:
        raise OfferWorkflowError("reason_too_long", "Reason cannot exceed 5000 characters.")
    return {"event_id": str(parsed_uuid), "reason": reason}


@_retry_transaction
def request_assignment_release(user_id, assignment_id, payload):
    data = validate_release_request_payload(payload)
    from django.contrib.auth import get_user_model
    user = get_user_model().objects.filter(pk=user_id, is_active=True).first()
    if user is None:
        raise OfferWorkflowError("authentication_required", "Please sign in.", 401)
    if has_backoffice_access(user):
        raise OfferWorkflowError("collector_portal_forbidden", "Use the Back Office portal with this account.", 403)
    profile = collector_profile(user_id)

    lookup = AccountAssignment.objects.filter(pk=assignment_id).values("account_id", "collector_id").first()
    if lookup is None:
        raise OfferWorkflowError("assignment_not_found", "Assigned account not found.", 404)

    with transaction.atomic():
        account = Account.objects.select_for_update(of=("self",)).get(pk=lookup["account_id"])
        prof = CollectorProfile.objects.select_for_update(of=("self",)).select_related("user").get(pk=lookup["collector_id"])
        assignment = AccountAssignment.objects.select_for_update(of=("self",)).get(pk=assignment_id)
        client = Client.objects.select_for_update().get(pk=account.client_id)
        grant = ClientAccess.objects.select_for_update().filter(user_id=user.pk, client_id=client.pk).first()

        # Recheck collector approval and active status under transaction lock
        if prof.approval_status != "approved" or not prof.user.is_active or prof.pk != profile.pk:
            raise OfferWorkflowError("collector_approval_required", "Only approved active collectors can request release.", 403)

        if assignment.collector_id != prof.pk:
            raise OfferWorkflowError("not_assigned_collector", "Only the assigned collector can request release.", 403)

        # Recheck collector client grant under transaction lock
        if grant is None or not grant.is_active or not client.is_active:
            raise OfferWorkflowError("client_access_required", "This account is outside your permitted client scope.", 403)

        # Check for existing request with this event_id under lock before checking active assignment
        existing = AssignmentReleaseRequest.objects.select_for_update().filter(event_id=data["event_id"]).first()
        if existing is not None:
            if existing.assignment_id == assignment.pk and existing.requested_by_id == user.pk and existing.reason == data["reason"]:
                return {"saved": True, "created": False, "request": existing}
            raise OfferWorkflowError("event_conflict", "This release request event ID cannot be reused with conflicting data.", 409)

        check_account_is_open(account, OfferWorkflowError)

        # If this is a new request, enforce active assignment and client permission flag
        if assignment.status != "active":
            raise OfferWorkflowError("not_active_assignment", "Only an active assignment can be requested for release.", 403)

        if not client.allow_collector_release:
            raise OfferWorkflowError("release_not_allowed", "Release requests are not permitted for this client.", 403)

        if assignment.release_requests.filter(status="requested").exists():
            raise OfferWorkflowError("pending_request_exists", "A release request is already pending for this assignment.", 409)

        release_req = AssignmentReleaseRequest.objects.create(
            assignment=assignment,
            requested_by=user,
            event_id=data["event_id"],
            reason=data["reason"],
            status="requested",
        )

        append_audit_event(
            actor=user,
            action="release_requested",
            entity_type="AssignmentReleaseRequest",
            entity_id=release_req.pk,
            account=account,
            changes={"status": {"before": "", "after": "requested"}},
        )
        from apps.core.notification_services import notify_client_reviewers
        notify_client_reviewers(account, "assignments.review_release_requests", f"release-request-{release_req.pk}",
            f"Release request for account {account.reference} needs a decision.", exclude_user_id=user.pk)

        return {"saved": True, "created": True, "request": release_req}


def validate_release_decision(decision, reason):
    if decision not in {"approve", "reject"}:
        raise OfferWorkflowError("invalid_decision", "Choose Approve release or Reject release.")
    reason = (reason or "").strip()
    if len(reason) > 5000:
        raise OfferWorkflowError("invalid_reason", "Review reason cannot exceed 5000 characters.")
    if decision == "reject" and not reason:
        raise OfferWorkflowError("reason_required", "A non-blank reason is required when rejecting a release request.")
    return reason


@_retry_transaction
def decide_release_request(user_id, request_id, decision, reason):
    reason = validate_release_decision(decision, reason)
    from django.contrib.auth import get_user_model
    user = get_user_model().objects.filter(pk=user_id, is_active=True).first()
    if user is None or not can_review_releases(user):
        raise OfferWorkflowError("permission_required", "Back Office release review permission is required.", 403)

    lookup = AssignmentReleaseRequest.objects.filter(pk=request_id).values(
        "assignment_id", "assignment__account_id", "assignment__collector_id", "requested_by_id"
    ).first()
    if lookup is None:
        raise OfferWorkflowError("request_not_found", "Release request not found.", 404)
    if lookup["requested_by_id"] == user.pk:
        raise OfferWorkflowError("self_review", "You cannot review your own release request.", 403)

    with transaction.atomic():
        account = Account.objects.select_for_update(of=("self",)).get(pk=lookup["assignment__account_id"])
        collector_prof = CollectorProfile.objects.select_for_update(of=("self",)).select_related("user").get(pk=lookup["assignment__collector_id"])
        assignment = AccountAssignment.objects.select_for_update(of=("self",)).get(pk=lookup["assignment_id"])
        client = Client.objects.select_for_update().get(pk=account.client_id)
        grant = ClientAccess.objects.select_for_update().filter(user_id=user.pk, client_id=client.pk).first()

        # Reload reviewer inside transaction before rechecking active status, superuser status, and permissions
        reviewer = get_user_model().objects.filter(pk=user_id).first()
        if reviewer is None or not reviewer.is_active or not can_review_releases(reviewer):
            raise OfferWorkflowError("permission_required", "Back Office release review permission is required.", 403)
        user = reviewer

        if grant is None or not grant.is_active or not client.is_active:
            raise OfferWorkflowError("client_access_required", "This release request is outside your permitted client scope.", 403)

        req = AssignmentReleaseRequest.objects.select_for_update().get(pk=request_id)

        target_status = "approved" if decision == "approve" else "rejected"
        if req.status in {"approved", "rejected", "superseded"}:
            if req.status == target_status and req.reviewed_by_id == user.pk and req.review_reason == reason:
                return {"saved": True, "changed": False, "status": req.status}
            raise OfferWorkflowError("review_conflict", "This release request has already been reviewed. Its decision cannot be overwritten.", 409)

        if req.status != "requested":
            raise OfferWorkflowError("invalid_state", "This release request cannot be reviewed in its current state.", 409)

        now = timezone.now()
        reviewed_time = max(now, req.created_at)

        if decision == "approve":
            if assignment.status != "active":
                raise OfferWorkflowError("assignment_not_active", "The assignment is no longer active.", 409)

            from apps.activities.models import FieldActivity, FollowUpTask
            if assignment.activities.filter(status="in_progress").exists():
                raise OfferWorkflowError("visit_in_progress", "Cannot approve release while a visit is in progress on this assignment.", 409)

            tasks = list(assignment.follow_up_tasks.select_for_update().filter(status__in=["requested", "pending"]))

            req.status = "approved"
            req.reviewed_by = user
            req.reviewed_at = reviewed_time
            req.review_reason = reason
            req.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_reason", "updated_at"])

            assignment.status = "released"
            assignment.ended_at = reviewed_time
            assignment.end_reason = f"Release request approved: {reason}" if reason else "Release request approved."
            assignment.next_action_due_at = None
            assignment.save(update_fields=["status", "ended_at", "end_reason", "next_action_due_at", "updated_at"])

            for task in tasks:
                prev_status = task.status
                task.status = "cancelled"
                task.decision_note = "Cancelled due to assignment release."
                task.save(update_fields=["status", "decision_note", "updated_at"])
                append_audit_event(
                    actor=user,
                    action="followup_cancelled",
                    entity_type="FollowUpTask",
                    entity_id=task.pk,
                    account=account,
                    changes={"status": {"before": prev_status, "after": "cancelled"}},
                )

            append_audit_event(
                actor=user,
                action="release_approved",
                entity_type="AssignmentReleaseRequest",
                entity_id=req.pk,
                account=account,
                changes={"status": {"before": "requested", "after": "approved"}},
            )
            append_audit_event(
                actor=user,
                action="assignment_released",
                entity_type="AccountAssignment",
                entity_id=assignment.pk,
                account=account,
                changes={"status": {"before": "active", "after": "released"}},
            )
            from apps.core.notification_services import notify_user
            notify_user(req.requested_by_id, account, f"release-{req.pk}-approved", f"Your release request for account {account.reference} was approved.")

            return {"saved": True, "changed": True, "status": "approved"}

        else:
            req.status = "rejected"
            req.reviewed_by = user
            req.reviewed_at = reviewed_time
            req.review_reason = reason
            req.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_reason", "updated_at"])

            append_audit_event(
                actor=user,
                action="release_rejected",
                entity_type="AssignmentReleaseRequest",
                entity_id=req.pk,
                account=account,
                changes={"status": {"before": "requested", "after": "rejected"}},
            )
            from apps.core.notification_services import notify_user
            notify_user(req.requested_by_id, account, f"release-{req.pk}-rejected", f"Your release request for account {account.reference} was rejected. Reason: {reason}")

            return {"saved": True, "changed": True, "status": "rejected"}


def can_manage_assignments(user):
    if not user or not user.is_authenticated or not user.is_active:
        return False
    if getattr(user, "is_superuser", False):
        return True
    for attr in ("_perm_cache", "_user_perm_cache", "_group_perm_cache"):
        if hasattr(user, attr):
            try:
                delattr(user, attr)
            except (AttributeError, KeyError):
                pass
    has_perm = getattr(user, "has_perm", None)
    return bool(has_perm and has_perm("assignments.manage_account_assignments"))


def compute_ownership_token(account):
    """
    Deterministic ownership-history token for an account:
    '<active_assignment_id>:<latest_assignment_id>:<total_assignment_count>'
    Detects if an unassigned account was assigned and released while form was open.
    """
    active = account.assignments.filter(status="active").first()
    active_id = active.pk if active else 0
    latest = account.assignments.order_by("-pk").first()
    latest_id = latest.pk if latest else 0
    total = account.assignments.count()
    return f"{active_id}:{latest_id}:{total}"


def assignable_accounts(user, query=None, state=None):
    if not can_manage_assignments(user):
        raise OfferWorkflowError("permission_required", "Back Office account assignment permission is required.", 403)
    client_ids_list = list(ClientAccess.objects.filter(user_id=user.pk, is_active=True, client__is_active=True).values_list("client_id", flat=True))
    active_assignments_prefetch = Prefetch(
        "assignments",
        queryset=AccountAssignment.objects.filter(status="active").select_related("collector__user"),
        to_attr="active_assignments",
    )
    base = Account.objects.filter(
        client_id__in=client_ids_list,
        source_system__in=settings.COLLECTION_OFFER_SOURCE_SYSTEMS,
        status="open",
        collection_address__verification_status="verified",
        collection_address__position__isnull=False,
        commission_rule__is_active=True,
    ).select_related("client", "customer", "collection_address", "commission_rule").prefetch_related(active_assignments_prefetch)

    if state == "unassigned":
        base = base.exclude(assignments__status="active")
    elif state == "assigned":
        base = base.filter(assignments__status="active")

    if query:
        query = query.strip()
        base = base.filter(
            Q(reference__icontains=query)
            | Q(customer__display_name__icontains=query)
            | Q(collection_address__address_line__icontains=query)
            | Q(assignments__collector__user__username__icontains=query, assignments__status="active")
        ).distinct()

    return base.order_by("-pk")


def eligible_collectors_for_account(account, staff_user):
    from django.contrib.auth import get_user_model
    from django.contrib.auth.models import Permission
    permission_filter = Q(pk__in=[])
    for permission_name in BACK_OFFICE_PERMISSIONS:
        app_label, codename = permission_name.split(".", 1)
        permission_filter |= Q(content_type__app_label=app_label, codename=codename)
    permissions = Permission.objects.filter(permission_filter)
    backoffice = get_user_model().objects.filter(
        Q(is_superuser=True) | Q(user_permissions__in=permissions) | Q(groups__permissions__in=permissions)
    ).values_list("pk", flat=True)

    candidates = CollectorProfile.objects.filter(
        approval_status="approved",
        user__is_active=True,
        user__client_accesses__client=account.client,
        user__client_accesses__is_active=True,
    ).exclude(user_id__in=backoffice).exclude(user_id=staff_user.pk)

    active_assignment = account.assignments.filter(status="active").first()
    if active_assignment:
        candidates = candidates.exclude(pk=active_assignment.collector_id)

    candidates = candidates.select_related("user", "location")
    now = timezone.now()

    result = []
    target_pos = account.collection_address.position if account.collection_address else None

    for prof in candidates:
        workload = prof.account_assignments.filter(status="active").count()
        recent_count = recent_acceptances(prof, now).count()
        loc = getattr(prof, "location", None)
        distance_m = None
        age_seconds = None
        captured_at = None
        if loc and loc.position and target_pos:
            val = CollectorLocation.objects.filter(pk=loc.pk).annotate(target_distance=Distance("position", target_pos)).values_list("target_distance", flat=True).first()
            if val is not None:
                distance_m = int(round(val.m))
            age_seconds = int((now - loc.captured_at).total_seconds())
            captured_at = loc.captured_at

        result.append({
            "id": prof.pk,
            "profile_id": prof.pk,
            "user_id": prof.user_id,
            "username": prof.user.username,
            "full_name": prof.user.get_full_name() or prof.user.username,
            "availability": prof.availability,
            "workload": workload,
            "recent_acceptances": recent_count,
            "distance_m": distance_m,
            "distance_km": f"{distance_m / 1000:.1f} km" if distance_m is not None else None,
            "observation_age_seconds": age_seconds,
            "location_captured_at": captured_at,
        })

    result.sort(key=lambda c: (c["distance_m"] is None, c["distance_m"] or 0, c["username"]))
    return result


@_retry_transaction
def assign_or_reassign_account(staff_user_id, account_id, collector_id, reason, event_id, submitted_ownership_token):
    if not event_id:
        raise OfferWorkflowError("invalid_event_id", "A valid event_id is required.")
    try:
        parsed_uuid = uuid.UUID(str(event_id))
    except (ValueError, TypeError):
        raise OfferWorkflowError("invalid_event_id", "A valid UUID event_id is required.")
    event_uuid = str(parsed_uuid)

    reason = (reason or "").strip()
    if not reason:
        raise OfferWorkflowError("reason_required", "A reason is required to assign or reassign an account.")
    if len(reason) > 5000:
        raise OfferWorkflowError("reason_too_long", "Reason cannot exceed 5000 characters.")

    submitted_ownership_token = (submitted_ownership_token or "").strip()
    if not submitted_ownership_token:
        raise OfferWorkflowError("ownership_token_required", "Ownership token is required.")

    try:
        collector_id = int(collector_id)
    except (ValueError, TypeError):
        raise OfferWorkflowError("invalid_collector", "A valid collector ID is required.")

    from django.contrib.auth import get_user_model
    from apps.activities.models import FieldActivity, FollowUpTask

    with transaction.atomic():
        reviewer = get_user_model().objects.filter(pk=staff_user_id, is_active=True).first()
        if reviewer is None or not can_manage_assignments(reviewer):
            raise OfferWorkflowError("permission_required", "Back Office account assignment permission is required.", 403)

        # 1. Lock Account
        account = Account.objects.select_for_update(of=("self",)).filter(pk=account_id).first()
        if account is None:
            raise OfferWorkflowError("account_not_found", "Account not found.", 404)

        # 2. Lock Collector Profiles in Ordered Sequence (PK order)
        current_assignment_collector_id = AccountAssignment.objects.filter(
            account=account, status="active"
        ).values_list("collector_id", flat=True).first()

        collector_pks = sorted(set(filter(None, [collector_id, current_assignment_collector_id])))
        profiles = list(CollectorProfile.objects.select_for_update(of=("self",)).select_related("user").filter(pk__in=collector_pks).order_by("pk"))

        recipient_profile = next((p for p in profiles if p.pk == collector_id), None)
        if recipient_profile is None:
            raise OfferWorkflowError("collector_not_found", "Collector profile not found.", 404)

        # 3. Lock Current Assignment (if any)
        current_assignment = account.assignments.select_for_update(of=("self",)).filter(status="active").first()

        # 4. Lock Client & Verify Active
        client = Client.objects.select_for_update().filter(pk=account.client_id).first()
        if client is None or not client.is_active:
            raise OfferWorkflowError("client_inactive", "Client is inactive or not found.", 409)

        # 5. Lock ClientAccess grants in Ordered Sequence (PK order)
        grant_user_ids = sorted(set([reviewer.pk, recipient_profile.user_id]))
        locked_grants = list(ClientAccess.objects.select_for_update().filter(
            client_id=client.pk, user_id__in=grant_user_ids
        ).order_by("pk"))

        # 6. Verify Reviewer Client Access under lock
        reviewer_grant = next((g for g in locked_grants if g.user_id == reviewer.pk and g.is_active), None)
        if reviewer_grant is None:
            raise OfferWorkflowError("reviewer_client_access_required", "You do not have active reviewer access to this client.", 403)

        # 7. Exact Idempotency / Replay Check (after reviewer client authorization and lock acquisition, before stale token check)
        existing_assignment = AccountAssignment.objects.select_for_update().filter(event_id=event_uuid).first()
        if existing_assignment is not None:
            snapshot = existing_assignment.policy_snapshot or {}
            matches = (
                snapshot.get("assigned_by_id") == reviewer.pk
                and existing_assignment.account_id == account_id
                and existing_assignment.collector_id == collector_id
                and snapshot.get("assignment_reason") == reason
                and snapshot.get("submitted_ownership_token") == submitted_ownership_token
            )
            if matches:
                return {"saved": True, "created": False, "assignment": existing_assignment, "reassigned": False}
            raise OfferWorkflowError("event_conflict", "This event ID has already been used with different parameters.", 409)

        # 8. Safe Address and Rule Relationship Lookups & Validation
        if account.collection_address_id is None:
            raise OfferWorkflowError("unverified_address", "Account collection address is missing.", 409)
        address = CustomerAddress.objects.select_for_update().filter(pk=account.collection_address_id).first()
        if address is None or address.verification_status != "verified" or address.position is None:
            raise OfferWorkflowError("unverified_address", "Account address must be verified.", 409)

        if account.commission_rule_id is None:
            raise OfferWorkflowError("inactive_commission_rule", "Account commission rule is missing.", 409)
        rule = CommissionRule.objects.select_for_update().filter(pk=account.commission_rule_id).first()
        if rule is None or not rule.is_active:
            raise OfferWorkflowError("inactive_commission_rule", "Account commission rule is inactive.", 409)

        # Validate Account state
        if account.source_system not in settings.COLLECTION_OFFER_SOURCE_SYSTEMS:
            raise OfferWorkflowError("workflow_not_enabled", "This account is not enabled for assignment workflows.", 409)
        if account.status != "open":
            raise OfferWorkflowError("account_not_open", "Only open accounts can be assigned.", 409)

        account.client = client
        account.collection_address = address
        account.commission_rule = rule
        try:
            validate_account_for_offers(account)
        except ValidationError as err:
            raise OfferWorkflowError("account_invalid", f"Account validation failed: {err.message if hasattr(err, 'message') else err}", 409)

        # 9. Check Ownership Token (Stale Check)
        current_token = compute_ownership_token(account)
        if current_token != submitted_ownership_token:
            raise OfferWorkflowError("stale_assignment", "The account assignment state has changed. Please refresh and review.", 409)

        # 10. Check In-Progress Visits on Account
        if FieldActivity.objects.filter(account=account, status="in_progress", kind="visit").exists():
            raise OfferWorkflowError("visit_in_progress", "Cannot reassign while a visit is in progress on this account.", 409)

        # 11. Validate Recipient Eligibility & Role Precedence
        if not recipient_profile.user.is_active or recipient_profile.approval_status != "approved":
            raise OfferWorkflowError("collector_approval_required", "Recipient must be an approved, active collector.", 403)
        if recipient_profile.user_id == reviewer.pk:
            raise OfferWorkflowError("cannot_assign_self", "Staff cannot assign accounts to themselves.", 403)
        if has_backoffice_access(recipient_profile.user):
            raise OfferWorkflowError("ordinary_collector_required", "Recipient must be an ordinary collector without Back Office access.", 403)

        if current_assignment and current_assignment.collector_id == recipient_profile.pk:
            raise OfferWorkflowError("already_assigned_to_collector", "Account is already assigned to this collector.", 409)

        # Check Recipient Client Access from locked grants
        recipient_grant = next((g for g in locked_grants if g.user_id == recipient_profile.user_id and g.is_active), None)
        if recipient_grant is None:
            raise OfferWorkflowError("recipient_client_access_required", "Recipient does not have active access to this client.", 403)

        # 12. Lock Offers, Tasks, and Releases
        pending_offers = list(account.offers.select_for_update().filter(status="pending").order_by("pk"))
        pending_releases = []
        pending_tasks = []
        if current_assignment:
            pending_releases = list(current_assignment.release_requests.select_for_update().filter(status="requested").order_by("pk"))
            pending_tasks = list(current_assignment.follow_up_tasks.select_for_update().filter(status__in=["requested", "pending"]).order_by("pk"))

        # Bounded audit text (maximum 100 characters per audit change scalar limit)
        audit_reason = reason[:100]

        # Withdraw pending offers
        for off in pending_offers:
            off.status = "withdrawn"
            off.save(update_fields=["status", "updated_at"])
            append_audit_event(
                actor=reviewer,
                action="offer_withdrawn",
                entity_type="AccountOffer",
                entity_id=off.pk,
                account=account,
                changes={"status": {"before": "pending", "after": "withdrawn"}},
            )
        withdrawn_count = len(pending_offers)

        # Handle Reassignment if previous assignment exists
        now = timezone.now()
        cancelled_tasks_count = 0
        superseded_release_id = None

        if current_assignment:
            for task in pending_tasks:
                prev_status = task.status
                task.status = "cancelled"
                task.decision_note = f"Cancelled due to account reassignment: {reason}"
                task.save(update_fields=["status", "decision_note", "updated_at"])
                append_audit_event(
                    actor=reviewer,
                    action="followup_cancelled",
                    entity_type="FollowUpTask",
                    entity_id=task.pk,
                    account=account,
                    changes={"status": {"before": prev_status, "after": "cancelled"}},
                )
                cancelled_tasks_count += 1

            for rel_req in pending_releases:
                rel_req.status = "superseded"
                rel_req.reviewed_by = reviewer
                rel_req.reviewed_at = now
                rel_req.review_reason = f"Superseded by reassignment: {reason}"
                rel_req.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_reason", "updated_at"])
                append_audit_event(
                    actor=reviewer,
                    action="release_superseded",
                    entity_type="AssignmentReleaseRequest",
                    entity_id=rel_req.pk,
                    account=account,
                    changes={
                        "status": {"before": "requested", "after": "superseded"},
                        "reason": audit_reason,
                    },
                )
                superseded_release_id = rel_req.pk

            prev_owner = current_assignment.collector.user.username
            prev_assignment_id = current_assignment.pk
            current_assignment.status = "reassigned"
            current_assignment.ended_at = now
            current_assignment.end_reason = reason
            current_assignment.next_action_due_at = None
            current_assignment.save(update_fields=["status", "ended_at", "end_reason", "next_action_due_at", "updated_at"])
            append_audit_event(
                actor=reviewer,
                action="assignment_reassigned",
                entity_type="AccountAssignment",
                entity_id=current_assignment.pk,
                account=account,
                changes={
                    "status": {"before": "active", "after": "reassigned"},
                    "reason": audit_reason,
                },
            )

        # Create new AccountAssignment (setting assigned_by=reviewer both as FK and in policy_snapshot)
        policy = _policy(client)
        policy["commission_terms"] = _terms(rule)
        policy["acceptance_limit"] = ROLLING_ACCEPTANCE_LIMIT
        policy["acceptance_window_hours"] = ROLLING_ACCEPTANCE_HOURS
        policy["assigned_by_id"] = reviewer.pk
        policy["assignment_reason"] = reason
        policy["submitted_ownership_token"] = submitted_ownership_token

        new_assignment = AccountAssignment.objects.create(
            account=account,
            collector=recipient_profile,
            offer=None,
            assigned_by=reviewer,
            target_address=address,
            commission_rule=rule,
            target_position=address.position.clone(),
            policy_snapshot=policy,
            first_action_due_at=now + timedelta(hours=policy["first_action_hours"]),
            event_id=event_uuid,
        )

        Notification.objects.get_or_create(
            dedup_key=f"assignment:{new_assignment.pk}:user:{recipient_profile.user_id}",
            defaults={
                "recipient": recipient_profile.user,
                "kind": "system",
                "account": account,
                "message": "An account has been assigned to you.",
            },
        )

        # Record Account Audit Event with bounded reason
        if current_assignment:
            append_audit_event(
                actor=reviewer,
                action="account_reassigned",
                entity_type="Account",
                entity_id=account.pk,
                account=account,
                changes={
                    "status": {"before": "open", "after": "open"},
                    "previous_assignment_id": prev_assignment_id,
                    "new_assignment_id": new_assignment.pk,
                    "previous_collector": prev_owner,
                    "new_collector": recipient_profile.user.username,
                    "reason": audit_reason,
                    "cancelled_followups_count": cancelled_tasks_count,
                    "superseded_release_request_id": superseded_release_id,
                    "withdrawn_offers_count": withdrawn_count,
                },
            )
        else:
            append_audit_event(
                actor=reviewer,
                action="account_assigned",
                entity_type="Account",
                entity_id=account.pk,
                account=account,
                changes={
                    "status": {"before": "open", "after": "open"},
                    "new_assignment_id": new_assignment.pk,
                    "collector": recipient_profile.user.username,
                    "reason": audit_reason,
                    "withdrawn_offers_count": withdrawn_count,
                },
            )

        return {"saved": True, "created": True, "assignment": new_assignment, "reassigned": current_assignment is not None}


def withdraw_pending_offers(account, reviewer):
    """Withdraw pending offers on an account. Expects account to be locked."""
    pending_offers = list(account.offers.select_for_update().filter(status="pending").order_by("pk"))
    for off in pending_offers:
        off.status = "withdrawn"
        off.save(update_fields=["status", "updated_at"])
        append_audit_event(
            actor=reviewer,
            action="offer_withdrawn",
            entity_type="AccountOffer",
            entity_id=off.pk,
            account=account,
            changes={"status": {"before": "pending", "after": "withdrawn"}},
        )
    return len(pending_offers)


def supersede_release_requests(account_or_assignment, reviewer, reason, now):
    """Supersede requested release requests on an account or assignment. Expects locked."""
    if hasattr(account_or_assignment, "account"):
        account = account_or_assignment.account
        pending_releases = list(account_or_assignment.release_requests.select_for_update().filter(status="requested").order_by("pk"))
    else:
        account = account_or_assignment
        pending_releases = list(AssignmentReleaseRequest.objects.select_for_update().filter(assignment__account=account, status="requested").order_by("pk"))
    audit_reason = reason[:100]
    for rel_req in pending_releases:
        rel_req.status = "superseded"
        rel_req.reviewed_by = reviewer
        rel_req.reviewed_at = now
        rel_req.review_reason = f"Superseded by account status transition: {reason}"[:5000]
        rel_req.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_reason", "updated_at"])
        append_audit_event(
            actor=reviewer,
            action="release_superseded",
            entity_type="AssignmentReleaseRequest",
            entity_id=rel_req.pk,
            account=account,
            changes={
                "status": {"before": "requested", "after": "superseded"},
                "reason": audit_reason,
            },
        )
    return len(pending_releases)


def complete_or_release_assignment(assignment, to_status, reason, reviewer, now, is_terminal=True):
    """Complete or release an active assignment. Expects assignment locked."""
    prev_status = assignment.status
    new_status = "completed" if is_terminal else "released"
    audit_action = "assignment_completed" if is_terminal else "assignment_released"

    assignment.status = new_status
    assignment.ended_at = now
    assignment.end_reason = f"Account {to_status}: {reason}"[:5000]
    assignment.next_action_due_at = None
    assignment.save(update_fields=["status", "ended_at", "end_reason", "next_action_due_at", "updated_at"])

    audit_changes = {"status": {"before": prev_status, "after": new_status}}
    if is_terminal:
        audit_changes["reason"] = reason[:100]

    append_audit_event(
        actor=reviewer,
        action=audit_action,
        entity_type="AccountAssignment",
        entity_id=assignment.pk,
        account=assignment.account,
        changes=audit_changes,
    )
    return assignment



"""Collector visits are evidence, never automatic financial or ownership decisions."""
import hashlib
import json
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone as utc_timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from uuid import UUID
from zoneinfo import ZoneInfo

from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.geos import Point
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Min, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.accounts.guards import check_account_is_open
from apps.accounts.models import Account, Client
from apps.user.models import ClientAccess
from apps.assignments.models import AccountAssignment
from apps.assignments.services import OfferWorkflowError, collector_profile, _retry_transaction
from apps.audit.services import append_audit_event
from apps.core.notification_services import notify_client_reviewers, notify_user
from apps.collection.models import PromiseToPay
from .models import FieldActivity, FollowUpTask

MANILA = ZoneInfo("Asia/Manila")


def can_review_visits(user):
    return bool(user.is_authenticated and user.is_active and user.has_perm("activities.change_fieldactivity"))


def reviewable_visits(user):
    if not can_review_visits(user):
        _error("Back Office review permission is required.","review_permission_required",403)
    clients=ClientAccess.objects.filter(user_id=user.pk,is_active=True,client__is_active=True).values_list("client_id",flat=True)
    return FieldActivity.objects.filter(kind="visit",status="submitted",account__client_id__in=clients).exclude(recorded_by_id=user.pk).select_related("account__customer","account__client","recorded_by","reviewed_by").order_by("occurred_at","pk")


def validate_review(decision,reason):
    if not isinstance(decision,str) or decision not in {"approve","reject"}:
        _error("Choose Approve report or Reject report.")
    return _text(reason,"Review reason",5000,decision=="reject")


@_retry_transaction
def review_visit(user_id,activity_id,decision,reason):
    reason=validate_review(decision,reason)
    user=get_user_model().objects.filter(pk=user_id).first()
    if user is None or not can_review_visits(user):
        _error("Back Office review permission is required.","review_permission_required",403)
    lookup=FieldActivity.objects.filter(pk=activity_id,kind="visit",status="submitted").values("account_id").first()
    if lookup is None:_error("Submitted visit not found.","visit_not_found",404)
    with transaction.atomic():
        account=Account.objects.select_for_update(of=("self",)).get(pk=lookup["account_id"])
        client=Client.objects.select_for_update().get(pk=account.client_id)
        grant=ClientAccess.objects.select_for_update().filter(user_id=user.pk,client=client,is_active=True).first()
        if grant is None or not client.is_active:_error("Submitted visit not found.","visit_not_found",404)
        activity=FieldActivity.objects.select_for_update().filter(pk=activity_id,account=account,kind="visit",status="submitted").first()
        if activity is None:_error("Submitted visit not found.","visit_not_found",404)
        if activity.recorded_by_id==user.pk:_error("You cannot review your own report.","self_review",403)
        # Fetch fresh permission evidence after lock waits rather than use a cached user.
        user=get_user_model().objects.filter(pk=user_id).first()
        if user is None or not can_review_visits(user):_error("Back Office review permission is required.","review_permission_required",403)
        status="approved" if decision=="approve" else "rejected"
        if activity.review_status in {"approved","rejected"}:
            if activity.review_status==status and activity.review_reason==reason and activity.reviewed_by_id==user.pk:
                return {"saved":True,"changed":False,"review_status":status}
            _error("This report has already been reviewed. Its decision cannot be overwritten.","review_conflict",409)
        if activity.review_status!="pending":
            _error("This legacy report is not enrolled for review.","legacy_review",409)
        activity.review_status=status;activity.review_reason=reason
        activity.reviewed_by=user;activity.reviewed_at=max(timezone.now(),activity.ended_at)
        activity.save(update_fields=["review_status","review_reason","reviewed_by","reviewed_at","updated_at"])
        append_audit_event(actor=user,action="visit_reviewed",entity_type="FieldActivity",entity_id=activity.pk,account=account,
            changes={"review_status":{"before":"pending","after":status}})
        suffix = f" Reason: {reason}" if reason else ""
        notify_user(activity.recorded_by_id, account, f"visit-review-{activity.pk}-{status}",
            f"Your visit report for account {account.reference} was {status}.{suffix}")
        return {"saved":True,"changed":True,"review_status":status}


def pending_followups(profile):
    """Only current ownership and client grants allow access to scheduled work."""
    from apps.assignments.services import client_ids
    if profile.approval_status != "approved":
        return FollowUpTask.objects.none()
    return FollowUpTask.objects.filter(
        assignee_id=profile.user_id, status="pending", assignment__collector=profile,
        assignment__status="active", account_id=F("assignment__account_id"),
        account__client_id__in=client_ids(profile),
    ).filter(Q(source_activity__isnull=True) | Q(source_activity__account_id=F("account_id"), source_activity__assignment_id=F("assignment_id"))).select_related("account__customer", "account__client").order_by("due_at", "pk")


def followup_day_end(now):
    tomorrow=now.astimezone(MANILA).date()+timedelta(days=1)
    return datetime.combine(tomorrow,time.min,tzinfo=MANILA).astimezone(utc_timezone.utc)


def followup_counts(tasks, now):
    end=followup_day_end(now)
    return tasks.aggregate(total=Count("pk"), overdue=Count("pk",filter=Q(due_at__lt=now)), today=Count("pk",filter=Q(due_at__gte=now,due_at__lt=end)), upcoming=Count("pk",filter=Q(due_at__gte=end)))


def followup_period(task, now):
    if task.due_at < now:
        return "overdue"
    return "today" if task.due_at < followup_day_end(now) else "upcoming"


def _error(message, code="invalid_visit", status=400):
    raise OfferWorkflowError(code, message, status)


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


def validate_completion(payload, now, check_dates=True):
    if not isinstance(payload, dict) or set(payload) != {"outcome", "notes", "promise", "follow_up"}:
        _error("Provide the outcome, notes, promise and follow-up fields only.")
    outcome = payload["outcome"]
    if not isinstance(outcome, str) or outcome not in FieldActivity.Outcome.values:
        _error("Choose a valid visit outcome.")
    result = {"outcome": outcome, "notes": _text(payload["notes"], "Notes", 5000, outcome in {"wrong_address", "other", "disputed"}), "promise": None, "follow_up": None}
    promise = payload["promise"]
    if outcome == "promise_to_pay":
        if not isinstance(promise, dict) or set(promise) != {"amount", "due_date"}:
            _error("Provide the promised amount and date.")
        try:
            raw = promise["amount"]
            if not isinstance(raw, str) or len(raw) > 20:
                raise ValueError
            amount = Decimal(raw)
            if not amount.is_finite() or not Decimal("0.01") <= amount <= Decimal("999999999999.99") or amount != amount.quantize(Decimal("0.01")):
                raise ValueError
            due_date = date.fromisoformat(promise["due_date"])
            if check_dates and due_date < now.astimezone(MANILA).date():
                raise ValueError
        except (ValueError, TypeError, InvalidOperation):
            _error("Enter a positive promised amount with at most two decimal places and a date of today or later.")
        result["promise"] = {"amount": format(amount, ".2f"), "due_date": due_date.isoformat()}
    elif promise is not None:
        _error("A promise belongs only to the promise-to-pay outcome.")
    follow_up = payload["follow_up"]
    if follow_up is not None:
        if not isinstance(follow_up, dict) or set(follow_up) != {"kind", "due_at"} or not isinstance(follow_up["kind"], str) or follow_up["kind"] not in ({"revisit"} if check_dates else {"revisit", "call", "promise_check"}):
            _error("Choose a follow-up action and date.")
        if follow_up["kind"] == "promise_check" and outcome != "promise_to_pay":
            _error("A promise check requires a promise-to-pay outcome.")
        due_at = _datetime(follow_up["due_at"])
        if check_dates and due_at <= now:
            _error("Schedule the follow-up for a future date and time.")
        result["follow_up"] = {"kind": follow_up["kind"], "due_at": due_at.isoformat()}
    return result


def _fingerprint(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_start(payload):
    if not isinstance(payload, dict) or set(payload) != {"event_id", "location", "location_issue_reason"}:
        _error("Provide the visit request, location and location explanation only.")
    try:
        if not isinstance(payload["event_id"], str):
            raise ValueError
        event_id = UUID(payload["event_id"])
    except (ValueError, TypeError, AttributeError):
        _error("Reload the page to create a valid visit request.")
    reason = _text(payload["location_issue_reason"], "Location explanation", 500)
    location = payload["location"]
    if location is None:
        if not reason:
            _error("Explain why location evidence is unavailable.", "location_reason_required")
        return event_id, None, reason
    if not isinstance(location, dict) or set(location) != {"latitude", "longitude", "accuracy_m", "captured_at"}:
        _error("Provide a valid location observation.")
    values = {}
    for key in ("latitude", "longitude", "accuracy_m"):
        raw = location[key]
        if isinstance(raw, bool) or not isinstance(raw, (int, float, Decimal)):
            _error("Location coordinates and accuracy must be finite numbers.")
        try:
            value = Decimal(str(raw))
            if not value.is_finite():
                raise ValueError
        except (InvalidOperation, ValueError):
            _error("Location coordinates and accuracy must be finite numbers.")
        values[key] = value
    if not -90 <= values["latitude"] <= 90 or not -180 <= values["longitude"] <= 180 or not 0 <= values["accuracy_m"] <= Decimal("999999.99"):
        _error("Location coordinates or accuracy are outside valid bounds.")
    return event_id, {"position": Point(float(values["longitude"]), float(values["latitude"]), srid=4326), "accuracy_m": values["accuracy_m"].quantize(Decimal(".01"), rounding=ROUND_CEILING), "location_captured_at": _datetime(location["captured_at"])}, reason


@contextmanager
def _authorized_assignment(user_id, assignment_id):
    lookup = AccountAssignment.objects.filter(pk=assignment_id, collector__user_id=user_id).values("account_id").first()
    if lookup is None:
        _error("Assigned account not found.", "assignment_not_found", 404)
    with transaction.atomic():
        # Same parent lock order as offer acceptance: account, collector, assignment.
        account = Account.objects.select_for_update(of=("self",)).select_related("client").get(pk=lookup["account_id"])
        profile = collector_profile(user_id, lock=True)
        assignment = AccountAssignment.objects.select_for_update(of=("self",)).filter(pk=assignment_id, account_id=account.pk, collector=profile, status="active").first()
        account.client = Client.objects.select_for_update().get(pk=account.client_id)
        grant = ClientAccess.objects.select_for_update(of=("self",)).filter(user_id=user_id, client_id=account.client_id, is_active=True).first()
        if assignment is None or grant is None or not account.client.is_active:
            _error("This account is no longer available to you.", "assignment_unavailable", 404)
        check_account_is_open(account, OfferWorkflowError)
        yield assignment, account


def _activity(assignment, user_id, activity_id):
    activity = FieldActivity.objects.select_for_update().filter(pk=activity_id, assignment=assignment, account_id=assignment.account_id, recorded_by_id=user_id, kind="visit").first()
    if activity is None:
        _error("Visit not found.", "visit_not_found", 404)
    return activity


def _audit(assignment, activity, action):
    append_audit_event(actor=assignment.collector.user, action=action, entity_type="FieldActivity", entity_id=activity.pk, account=activity.account, changes={"status": activity.status})


def activity_payload(activity):
    return {"id": activity.pk, "status": activity.status, "started_at": activity.occurred_at.isoformat(), "ended_at": activity.ended_at.isoformat() if activity.ended_at else None,
            "accuracy_m": str(activity.accuracy_m) if activity.accuracy_m is not None else None, "distance_m": str(activity.distance_m) if activity.distance_m is not None else None,
            "location_issue_reason": activity.location_issue_reason, "review_status": activity.review_status}


def revisit_tasks(assignment, user_id, latest):
    return FollowUpTask.objects.filter(account_id=assignment.account_id, assignment=assignment,
        assignee_id=user_id, source_activity=latest, kind="revisit", status="pending").exclude(
        resulting_visits__status__in=["in_progress", "submitted"]).order_by("due_at", "pk")


def task_window_state(task, now):
    if task.available_from is None:
        return "ready" if task.due_at <= now else "waiting"  # Preserved legacy policy.
    if now > task.due_at:
        return "overdue"
    return "ready" if task.available_from <= now else "waiting"


def visit_gate(assignment, user_id, now):
    latest=FieldActivity.objects.filter(assignment=assignment, account_id=assignment.account_id,
        recorded_by_id=user_id, kind="visit", status="submitted").order_by("-ended_at", "-pk").first()
    result={"allowed":False,"state":"schedule_required","latest_id":latest.pk if latest else None,"next_due_at":None,"deadline_at":None,"task_id":None}
    if FieldActivity.objects.filter(recorded_by_id=user_id,kind="visit",status="in_progress").exclude(assignment=assignment).exists():
        result["state"]="another_visit"
        return result
    tasks=FollowUpTask.objects.filter(account_id=assignment.account_id,assignment=assignment,assignee_id=user_id,
        kind="revisit",status="pending").exclude(resulting_visits__status__in=["in_progress","submitted"])
    tasks=tasks.filter(source_activity=latest) if latest else tasks.filter(source_activity__isnull=True)
    task=tasks.order_by("due_at","pk").first()
    if task:
        state=task_window_state(task,now)
        result.update(allowed=state=="ready",state=state,next_due_at=(task.available_from or task.due_at).isoformat(),deadline_at=task.due_at.isoformat(),task_id=task.pk)
    elif latest is None:
        result.update(allowed=True,state="first_visit")
    elif FollowUpTask.objects.filter(assignment=assignment,account_id=assignment.account_id,source_activity=latest,assignee_id=user_id,kind="revisit",status="requested").exists():
        result["state"]="requested"
    return result


def _refresh_next_action(assignment):
    assignment.next_action_due_at=assignment.follow_up_tasks.filter(account_id=assignment.account_id,status="pending").aggregate(earliest=Min("due_at"))["earliest"]
    assignment.save(update_fields=["next_action_due_at","updated_at"])


def _task_payload(task):
    return {"id":task.pk,"due_at":task.due_at.isoformat(),"available_from":task.available_from.isoformat() if task.available_from else None,"kind":task.kind,"status":task.status}


@_retry_transaction
def schedule_revisit(user_id, assignment_id, activity_id, payload):
    if not isinstance(payload,dict) or set(payload)!={"due_at"}:
        _error("Provide the next visit date and time only.")
    due_at=_datetime(payload["due_at"])
    with _authorized_assignment(user_id,assignment_id) as (assignment,account):
        activity=_activity(assignment,user_id,activity_id)
        gate=visit_gate(assignment,user_id,timezone.now())
        if activity.status!="submitted" or gate["latest_id"]!=activity.pk or assignment.activities.filter(kind="visit",status="in_progress").exists():
            _error("Reload the account before scheduling its next visit.","visit_conflict",409)
        existing=FollowUpTask.objects.select_for_update().filter(assignment=assignment,account=account,assignee_id=user_id,source_activity=activity,kind="revisit",status__in=["requested","pending"]).first()
        if existing:
            if existing.due_at!=due_at:
                _error("A next visit is already scheduled. Reload to see its date.","revisit_exists",409)
            return {"saved":True,"created":False,"task":_task_payload(existing),"gate":gate}
        if due_at<=timezone.now():
            _error("Schedule the next visit for a future date and time.")
        task=FollowUpTask.objects.create(account=account,assignment=assignment,source_activity=activity,assignee_id=user_id,kind="revisit",due_at=due_at,status="requested",requested_by_id=user_id)
        _refresh_next_action(assignment)
        append_audit_event(actor=assignment.collector.user,action="followup_requested",entity_type="FollowUpTask",entity_id=task.pk,account=account,changes={"status":"requested"})
        notify_client_reviewers(account, "activities.change_followuptask", f"followup-request-{task.pk}",
            f"Follow-up request for account {account.reference} is awaiting scheduling.", exclude_user_id=user_id)
        return {"saved":True,"created":True,"task":_task_payload(task),"gate":visit_gate(assignment,user_id,timezone.now())}


@_retry_transaction
def start_visit(user_id, assignment_id, payload):
    event_id, evidence, reason = validate_start(payload)
    with _authorized_assignment(user_id, assignment_id) as (assignment, account):
        existing = FieldActivity.objects.filter(event_id=event_id).first()
        if existing is not None:
            if existing.assignment_id != assignment.pk or existing.account_id != account.pk or existing.recorded_by_id != user_id or existing.kind != "visit":
                _error("This visit request cannot be reused.", "event_conflict", 409)
            return {"saved": True, "created": False, "visit": activity_payload(existing),"gate":visit_gate(assignment,user_id,timezone.now())}
        ongoing = FieldActivity.objects.filter(assignment=assignment, kind="visit", status="in_progress").first()
        if ongoing is not None:
            if ongoing.recorded_by_id != user_id:
                _error("An earlier visit needs supervisor attention before another visit can start.", "visit_conflict", 409)
            return {"saved": True, "created": False, "visit": activity_payload(ongoing),"gate":visit_gate(assignment,user_id,timezone.now())}
        now = timezone.now()
        gate=visit_gate(assignment,user_id,now)
        if gate["state"]=="another_visit":
            _error("Finish or cancel your current visit before starting work on another account.","collector_visit_in_progress",409)
        if not gate["allowed"]:
            _error("Wait until the scheduled revisit time." if gate["state"]=="waiting" else "Ask Back Office to authorize the next visit, or contact them about an overdue schedule.","revisit_not_due" if gate["state"]=="waiting" else "revisit_required",409)
        scheduled=None
        if gate["task_id"]:
            scheduled=FollowUpTask.objects.select_for_update().filter(pk=gate["task_id"],account=account,assignment=assignment,
                assignee_id=user_id,source_activity_id=gate["latest_id"],kind="revisit",status="pending").first()
            if scheduled is None or task_window_state(scheduled,now)!="ready":
                _error("The scheduled revisit changed. Reload the account.","revisit_unavailable",409)
        if evidence is not None:
            client = account.client
            age = (now - evidence["location_captured_at"]).total_seconds()
            concerns = age > client.gps_max_age_seconds or age < -client.gps_max_clock_skew_seconds or evidence["accuracy_m"] > client.gps_max_accuracy_m
            if concerns and not reason:
                _error("This location is old, inaccurate or has a clock issue. Retry location detection or provide an explanation.", "location_reason_required")
            measured = AccountAssignment.objects.filter(pk=assignment.pk).annotate(measured=Distance("target_position", evidence["position"], spheroid=True)).values_list("measured", flat=True).get()
            evidence["distance_m"] = Decimal(str(measured.m)).quantize(Decimal(".01"))
        try:
            with transaction.atomic():
                activity = FieldActivity.objects.create(account=account, assignment=assignment, scheduled_followup=scheduled, recorded_by_id=user_id, event_id=event_id, occurred_at=now, location_issue_reason=reason, **(evidence or {}))
        except IntegrityError as exc:
            if FieldActivity.objects.filter(event_id=event_id).exists():
                _error("This visit request cannot be reused.", "event_conflict", 409)
            if getattr(getattr(exc.__cause__,"diag",None),"constraint_name",None)=="activity_one_collector_visit":
                _error("Finish or cancel your current visit before starting work on another account.","collector_visit_in_progress",409)
            if getattr(getattr(exc.__cause__,"diag",None),"constraint_name",None)=="activity_one_visit_per_followup":
                _error("This scheduled revisit already has a visit. Reload the account.","revisit_unavailable",409)
            raise
        _audit(assignment, activity, "visit_started")
        return {"saved": True, "created": True, "visit": activity_payload(activity),"gate":visit_gate(assignment,user_id,now)}


@_retry_transaction
def submit_visit(user_id, assignment_id, activity_id, payload):
    with _authorized_assignment(user_id, assignment_id) as (assignment, account):
        activity = _activity(assignment, user_id, activity_id)
        now = timezone.now()
        normalized = validate_completion(payload, now, check_dates=activity.status != "submitted")
        fingerprint = _fingerprint(normalized)
        if activity.status == "submitted":
            if activity.submission_fingerprint != fingerprint:
                _error("This visit was already submitted with different information.", "visit_conflict", 409)
            return {"saved": True, "changed": False, "visit": activity_payload(activity), "gate":visit_gate(assignment,user_id,now)}
        if activity.status != "in_progress":
            _error("This visit is no longer in progress.", "visit_closed", 409)
        scheduled=None
        if activity.scheduled_followup_id:
            scheduled=FollowUpTask.objects.select_for_update().filter(pk=activity.scheduled_followup_id,account=account,assignment=assignment,assignee_id=user_id,kind="revisit",status="pending").first()
            if scheduled is None:
                _error("The scheduled revisit changed. Contact your supervisor before submitting.","revisit_unavailable",409)
        activity.status = "submitted"
        activity.review_status = "pending"
        activity.outcome = normalized["outcome"]
        activity.notes = normalized["notes"]
        activity.ended_at = max(now, activity.occurred_at)
        activity.submission_fingerprint = fingerprint
        activity.save(update_fields=["status", "review_status", "outcome", "notes", "ended_at", "submission_fingerprint", "updated_at"])
        if scheduled:
            scheduled.status="completed";scheduled.completed_at=activity.ended_at
            scheduled.save(update_fields=["status","completed_at","updated_at"])
            append_audit_event(actor=assignment.collector.user,action="revisit_completed",entity_type="FollowUpTask",entity_id=scheduled.pk,account=account,changes={"status":"completed"})
        if normalized["promise"]:
            PromiseToPay.objects.create(activity=activity, **normalized["promise"])
        if normalized["follow_up"]:
            task=FollowUpTask.objects.create(account=account, assignment=assignment, source_activity=activity, assignee_id=user_id, requested_by_id=user_id,status="requested", **normalized["follow_up"])
            append_audit_event(actor=assignment.collector.user,action="followup_requested",entity_type="FollowUpTask",entity_id=task.pk,account=account,changes={"status":"requested"})
        if normalized["follow_up"] or scheduled:
            _refresh_next_action(assignment)
        _audit(assignment, activity, "visit_submitted")
        notify_client_reviewers(account, "activities.change_fieldactivity", f"visit-review-needed-{activity.pk}",
            f"Visit report for account {account.reference} is awaiting review.", exclude_user_id=user_id)
        if normalized["follow_up"]:
            notify_client_reviewers(account, "activities.change_followuptask", f"followup-request-{task.pk}",
                f"Follow-up request for account {account.reference} is awaiting scheduling.", exclude_user_id=user_id)
        return {"saved": True, "changed": True, "visit": activity_payload(activity), "gate":visit_gate(assignment,user_id,now)}


@_retry_transaction
def cancel_visit(user_id, assignment_id, activity_id, payload):
    if not isinstance(payload, dict) or set(payload) != {"reason"}:
        _error("Provide a cancellation reason only.")
    reason = _text(payload["reason"], "Cancellation reason", 500, True)
    with _authorized_assignment(user_id, assignment_id) as (assignment, account):
        activity = _activity(assignment, user_id, activity_id)
        if activity.status == "cancelled" and activity.cancel_reason == reason:
            return {"saved": True, "changed": False, "visit": activity_payload(activity), "gate":visit_gate(assignment,user_id,timezone.now())}
        if activity.status != "in_progress":
            _error("This visit is no longer in progress.", "visit_closed", 409)
        activity.status = "cancelled"
        activity.cancel_reason = reason
        activity.ended_at = max(timezone.now(), activity.occurred_at)
        activity.save(update_fields=["status", "cancel_reason", "ended_at", "updated_at"])
        _audit(assignment, activity, "visit_cancelled")
        return {"saved": True, "changed": True, "visit": activity_payload(activity), "gate":visit_gate(assignment,user_id,timezone.now())}


def can_schedule_followups(user):
    return bool(user.is_authenticated and user.is_active and user.has_perm("activities.change_followuptask"))


def schedulable_assignments(user):
    if not can_schedule_followups(user):
        _error("Back Office scheduling permission is required.","schedule_permission_required",403)
    clients=ClientAccess.objects.filter(user=user,is_active=True,client__is_active=True).values_list("client_id",flat=True)
    return AccountAssignment.objects.filter(status="active",account__client_id__in=clients,
        collector__approval_status="approved",collector__user__is_active=True).exclude(collector__user_id=user.pk).select_related("account__client","account__customer","collector__user").order_by("account__reference","pk")


def validate_schedule_window(start, deadline, now):
    start=_datetime(start);deadline=_datetime(deadline)
    if start < now or deadline <= start:
        _error("Choose a start time of now or later and a completion deadline after it.")
    return start,deadline


@_retry_transaction
def decide_followup(user_id,assignment_id,decision,available_from=None,due_at=None,note="",task_id=None):
    if not isinstance(decision,str) or decision not in {"authorize","reject"}:
        _error("Choose Authorize schedule or Reject request.")
    note=_text(note,"Decision note",5000,decision=="reject")
    user=get_user_model().objects.filter(pk=user_id).first()
    if user is None or not can_schedule_followups(user):
        _error("Back Office scheduling permission is required.","schedule_permission_required",403)
    lookup=AccountAssignment.objects.filter(pk=assignment_id).values("account_id","collector_id").first()
    if lookup is None:_error("Assigned account not found.","assignment_not_found",404)
    with transaction.atomic():
        account=Account.objects.select_for_update(of=("self",)).get(pk=lookup["account_id"])
        from apps.user.models import CollectorProfile
        profile=CollectorProfile.objects.select_for_update().get(pk=lookup["collector_id"])
        assignment=AccountAssignment.objects.select_for_update().filter(pk=assignment_id,account=account,collector=profile,status="active").first()
        client=Client.objects.select_for_update().get(pk=account.client_id)
        grant=ClientAccess.objects.select_for_update().filter(user_id=user_id,client=client,is_active=True).first()
        collector_grant=ClientAccess.objects.select_for_update().filter(user_id=profile.user_id,client=client,is_active=True).first()
        user=get_user_model().objects.filter(pk=user_id).first()
        if user is None or not can_schedule_followups(user):
            _error("Back Office scheduling permission is required.","schedule_permission_required",403)
        if assignment is None or grant is None or collector_grant is None or not client.is_active or profile.approval_status!="approved" or not profile.user.is_active:
            _error("Assigned account not found.","assignment_not_found",404)
        if profile.user_id==user_id:
            _error("You cannot authorize your own follow-up.","self_schedule",403)
        latest=assignment.activities.filter(account=account,recorded_by_id=profile.user_id,kind="visit",status="submitted").order_by("-ended_at","-pk").first()
        task=None
        if task_id is not None:
            task=FollowUpTask.objects.select_for_update().filter(pk=task_id,account=account,assignment=assignment,assignee_id=profile.user_id,kind="revisit").first()
            if task is None:_error("Follow-up request not found.","request_not_found",404)
        if decision=="reject":
            if task is None:_error("Choose a follow-up request to reject.")
            if task.status=="rejected" and task.decision_note==note:
                return {"saved":True,"changed":False,"task":_task_payload(task)}
            if task.status!="requested":_error("This request has already been decided.","schedule_conflict",409)
            task.status="rejected";task.decision_note=note
            task.save(update_fields=["status","decision_note","updated_at"])
            append_audit_event(actor=user,action="followup_rejected",entity_type="FollowUpTask",entity_id=task.pk,account=account,changes={"status":"rejected"})
            notify_user(profile.user_id, account, f"followup-{task.pk}-rejected", f"Your follow-up request for account {account.reference} was rejected. Reason: {note}")
            return {"saved":True,"changed":True,"task":_task_payload(task)}
        start=_datetime(available_from);deadline=_datetime(due_at)
        if task is None:
            task=FollowUpTask.objects.select_for_update().filter(account=account,assignment=assignment,assignee_id=profile.user_id,source_activity=latest,kind="revisit",status__in=["requested","pending"]).order_by("pk").first()
        if task and task.status=="pending" and task.authorized_by_id==user_id and task.available_from==start and task.due_at==deadline and task.decision_note==note:
            return {"saved":True,"changed":False,"task":_task_payload(task)}
        if task and task.status!="requested":_error("An existing schedule cannot be overwritten.","schedule_conflict",409)
        if task and task.source_activity_id!=(latest.pk if latest else None):
            _error("This request belongs to an earlier visit. Reload the account.","schedule_conflict",409)
        if assignment.activities.filter(kind="visit",status="in_progress").exists():
            _error("A visit is already in progress. Schedule its follow-up after submission.","visit_in_progress",409)
        start,deadline=validate_schedule_window(available_from,due_at,timezone.now())
        other=FollowUpTask.objects.filter(assignment=assignment,account=account,assignee_id=profile.user_id,source_activity=latest,kind="revisit",status="pending")
        if task:other=other.exclude(pk=task.pk)
        if other.exists():_error("A visit is already scheduled for this report.","schedule_conflict",409)
        if task is None:
            task=FollowUpTask(account=account,assignment=assignment,source_activity=latest,assignee_id=profile.user_id,kind="revisit")
        task.status="pending";task.available_from=start;task.due_at=deadline
        task.authorized_by=user;task.authorized_at=timezone.now();task.decision_note=note
        task.save()
        _refresh_next_action(assignment)
        append_audit_event(actor=user,action="followup_authorized",entity_type="FollowUpTask",entity_id=task.pk,account=account,
            changes={"status":"pending","available_from":start.isoformat(),"due_at":deadline.isoformat()})
        notify_user(profile.user_id, account, f"followup-{task.pk}-authorized", f"Your follow-up for account {account.reference} was scheduled.")
        return {"saved":True,"changed":True,"task":_task_payload(task)}


CONTACT_ATTEMPT_KINDS = {"phone", "sms", "email"}
CONTACT_ATTEMPT_OUTCOMES = {"contacted", "unavailable", "disputed", "other"}


def validate_contact_attempt(payload):
    if not isinstance(payload, dict) or set(payload.keys()) != {"event_id", "kind", "outcome", "notes"}:
        _error("Provide the event_id, kind, outcome and notes fields only.", "invalid_payload", 400)

    raw_event_id = payload["event_id"]
    if not isinstance(raw_event_id, str):
        _error("Provide a valid event ID.", "invalid_event_id", 400)
    try:
        event_id = UUID(raw_event_id)
    except (ValueError, TypeError, AttributeError):
        _error("Provide a valid event ID.", "invalid_event_id", 400)

    kind = payload["kind"]
    if not isinstance(kind, str) or kind not in CONTACT_ATTEMPT_KINDS:
        _error("Choose a valid contact method (phone, sms, or email).", "invalid_kind", 400)

    outcome = payload["outcome"]
    if not isinstance(outcome, str) or outcome not in CONTACT_ATTEMPT_OUTCOMES:
        _error("Choose a valid contact attempt outcome.", "invalid_outcome", 400)

    raw_notes = payload["notes"]
    if not isinstance(raw_notes, str):
        _error("Notes must be text of no more than 5000 characters.", "invalid_notes", 400)
    notes = raw_notes.strip()
    if not notes:
        _error("Provide notes for this contact attempt.", "blank_notes", 400)
    if len(raw_notes) > 5000 or len(notes) > 5000:
        _error("Notes must be text of no more than 5000 characters.", "invalid_notes", 400)

    return event_id, kind, outcome, notes


def contact_attempt_payload(activity):
    return {
        "id": activity.pk,
        "event_id": str(activity.event_id),
        "kind": activity.kind,
        "outcome": activity.outcome,
        "status": activity.status,
        "occurred_at": activity.occurred_at.isoformat(),
        "ended_at": activity.ended_at.isoformat() if activity.ended_at else None,
        "notes": activity.notes,
    }


@_retry_transaction
def record_contact_attempt(user_id, assignment_id, payload):
    event_id, kind, outcome, notes = validate_contact_attempt(payload)
    with _authorized_assignment(user_id, assignment_id) as (assignment, account):
        existing = FieldActivity.objects.filter(event_id=event_id).first()
        if existing is not None:
            if (
                existing.assignment_id != assignment.pk
                or existing.account_id != account.pk
                or existing.recorded_by_id != user_id
                or existing.kind != kind
                or existing.outcome != outcome
                or existing.notes != notes
            ):
                _error("This contact attempt request cannot be reused with different information.", "event_conflict", 409)
            return {"saved": True, "created": False, "attempt": contact_attempt_payload(existing)}

        now = timezone.now()
        normalized = {
            "account": account.pk,
            "assignment": assignment.pk,
            "collector": user_id,
            "kind": kind,
            "outcome": outcome,
            "notes": notes,
        }
        fingerprint = _fingerprint(normalized)
        try:
            with transaction.atomic():
                activity = FieldActivity.objects.create(
                    account=account,
                    assignment=assignment,
                    recorded_by_id=user_id,
                    event_id=event_id,
                    kind=kind,
                    status=FieldActivity.Status.SUBMITTED,
                    review_status="not_required",
                    outcome=outcome,
                    notes=notes,
                    occurred_at=now,
                    ended_at=now,
                    submission_fingerprint=fingerprint,
                )
        except IntegrityError:
            existing = FieldActivity.objects.filter(event_id=event_id).first()
            if existing is not None:
                if (
                    existing.assignment_id != assignment.pk
                    or existing.account_id != account.pk
                    or existing.recorded_by_id != user_id
                    or existing.kind != kind
                    or existing.outcome != outcome
                    or existing.notes != notes
                ):
                    _error("This contact attempt request cannot be reused with different information.", "event_conflict", 409)
                return {"saved": True, "created": False, "attempt": contact_attempt_payload(existing)}
            raise

        append_audit_event(
            actor=assignment.collector.user,
            action="contact_attempt_recorded",
            entity_type="FieldActivity",
            entity_id=activity.pk,
            account=account,
            changes={"status": activity.status, "kind": kind, "outcome": outcome},
        )
        return {"saved": True, "created": True, "attempt": contact_attempt_payload(activity)}


def cancel_in_progress_visit(account_or_assignment, reviewer, now):
    """Cancel any in-progress visit on the account or assignment. Expects locked."""
    if hasattr(account_or_assignment, "account"):
        account = account_or_assignment.account
        in_progress = account_or_assignment.activities.select_for_update().filter(status="in_progress", kind="visit").first()
    else:
        account = account_or_assignment
        in_progress = FieldActivity.objects.select_for_update().filter(account=account, status="in_progress", kind="visit").first()
    if in_progress is not None:
        in_progress.status = "cancelled"
        in_progress.ended_at = now
        in_progress.cancel_reason = "account_status_changed"
        in_progress.outcome = ""
        in_progress.submission_fingerprint = ""
        in_progress.review_status = "not_required"
        in_progress.save(update_fields=[
            "status", "ended_at", "cancel_reason", "outcome",
            "submission_fingerprint", "review_status", "updated_at"
        ])
        append_audit_event(
            actor=reviewer,
            action="visit_cancelled",
            entity_type="FieldActivity",
            entity_id=in_progress.pk,
            account=account,
            changes={"status": {"before": "in_progress", "after": "cancelled"}},
        )
        return 1
    return 0


def cancel_pending_followups(account_or_assignment, reviewer, reason, now):
    """Cancel requested and pending follow-up tasks on an account or assignment."""
    if hasattr(account_or_assignment, "account"):
        account = account_or_assignment.account
        pending_tasks = list(account_or_assignment.follow_up_tasks.select_for_update().filter(status__in=["requested", "pending"]).order_by("pk"))
    else:
        account = account_or_assignment
        pending_tasks = list(FollowUpTask.objects.select_for_update().filter(account=account, status__in=["requested", "pending"]).order_by("pk"))
    for task in pending_tasks:
        prev_status = task.status
        task.status = "cancelled"
        task.completed_at = None
        task.decision_note = f"Cancelled due to account status change: {reason}"[:5000]
        task.save(update_fields=["status", "decision_note", "completed_at", "updated_at"])
        append_audit_event(
            actor=reviewer,
            action="followup_cancelled",
            entity_type="FollowUpTask",
            entity_id=task.pk,
            account=account,
            changes={"status": {"before": prev_status, "after": "cancelled"}},
        )
    return len(pending_tasks)


@_retry_transaction
def sync_offline_visit(user_id, assignment_id, payload, photo_file=None):
    from apps.activities.models import Document
    from apps.collection.services import validate_uploaded_document, save_private_document

    try:
        event_id = UUID(str(payload.get("event_id")))
    except (ValueError, TypeError, AttributeError):
        _error("Valid visit event ID is required.", "invalid_event_id", 400)

    # Idempotency check:
    existing = FieldActivity.objects.filter(event_id=event_id).first()
    if existing is not None:
        if existing.assignment_id != assignment_id or existing.recorded_by_id != user_id or existing.kind != "visit":
            _error("This visit request cannot be reused.", "event_conflict", 409)
        return {
            "saved": True,
            "created": False,
            "activity_id": existing.pk,
            "visit": activity_payload(existing),
        }

    with _authorized_assignment(user_id, assignment_id) as (assignment, account):
        now = timezone.now()
        occurred_at = parse_datetime(str(payload.get("occurred_at", ""))) or now
        ended_at = parse_datetime(str(payload.get("ended_at", ""))) or now
        if ended_at < occurred_at:
            ended_at = occurred_at

        completion_data = {
            "outcome": payload.get("outcome"),
            "notes": payload.get("notes", ""),
            "promise": payload.get("promise"),
            "follow_up": payload.get("follow_up"),
        }
        normalized = validate_completion(completion_data, now, check_dates=False)
        fingerprint = _fingerprint(normalized)

        # Handle position if passed
        evidence = {}
        reason = (payload.get("location_issue_reason") or "Offline visit submission")[:500]
        lat = payload.get("latitude")
        lng = payload.get("longitude")
        acc_m = payload.get("accuracy_m")
        if lat is not None and lng is not None and acc_m is not None:
            try:
                lat_d = Decimal(str(lat))
                lng_d = Decimal(str(lng))
                acc_d = Decimal(str(acc_m)).quantize(Decimal(".01"))
                if -90 <= lat_d <= 90 and -180 <= lng_d <= 180 and acc_d >= 0:
                    pos = Point(float(lng_d), float(lat_d), srid=4326)
                    measured = AccountAssignment.objects.filter(pk=assignment.pk).annotate(
                        measured=Distance("target_position", pos, spheroid=True)
                    ).values_list("measured", flat=True).first()
                    dist_d = Decimal(str(measured.m)).quantize(Decimal(".01")) if measured else Decimal("0.00")
                    evidence = {
                        "position": pos,
                        "accuracy_m": acc_d,
                        "location_captured_at": occurred_at,
                        "distance_m": dist_d,
                    }
            except Exception:
                evidence = {}

        # Look for any existing in_progress visit on this assignment by this user
        in_progress = FieldActivity.objects.select_for_update().filter(
            assignment=assignment, kind="visit", status="in_progress", recorded_by_id=user_id
        ).first()

        scheduled = None
        gate = visit_gate(assignment, user_id, now)
        if gate.get("task_id"):
            scheduled = FollowUpTask.objects.select_for_update().filter(
                pk=gate["task_id"], account=account, assignment=assignment, assignee_id=user_id, kind="revisit", status="pending"
            ).first()

        if in_progress:
            activity = in_progress
            activity.status = "submitted"
            activity.review_status = "pending"
            activity.outcome = normalized["outcome"]
            activity.notes = normalized["notes"]
            activity.ended_at = ended_at
            activity.location_issue_reason = reason
            activity.submission_fingerprint = fingerprint
            for k, v in evidence.items():
                setattr(activity, k, v)
            activity.save()
        else:
            activity = FieldActivity.objects.create(
                account=account,
                assignment=assignment,
                scheduled_followup=scheduled,
                recorded_by_id=user_id,
                event_id=event_id,
                kind="visit",
                status="submitted",
                review_status="pending",
                outcome=normalized["outcome"],
                notes=normalized["notes"],
                occurred_at=occurred_at,
                ended_at=ended_at,
                location_issue_reason=reason,
                submission_fingerprint=fingerprint,
                **evidence
            )

        if scheduled:
            scheduled.status = "completed"
            scheduled.completed_at = activity.ended_at
            scheduled.save(update_fields=["status", "completed_at", "updated_at"])
            append_audit_event(actor=assignment.collector.user, action="revisit_completed", entity_type="FollowUpTask", entity_id=scheduled.pk, account=account, changes={"status": "completed"})

        if normalized["promise"]:
            PromiseToPay.objects.create(activity=activity, **normalized["promise"])

        if normalized["follow_up"]:
            task = FollowUpTask.objects.create(account=account, assignment=assignment, source_activity=activity, assignee_id=user_id, requested_by_id=user_id, status="requested", **normalized["follow_up"])
            append_audit_event(actor=assignment.collector.user, action="followup_requested", entity_type="FollowUpTask", entity_id=task.pk, account=account, changes={"status": "requested"})

        if normalized["follow_up"] or scheduled:
            _refresh_next_action(assignment)

        # Handle photo attachment if provided
        if photo_file:
            doc_meta = validate_uploaded_document(photo_file)
            rel_path = save_private_document(account.pk, photo_file, doc_meta)
            Document.objects.create(
                account=account,
                activity=activity,
                uploaded_by=assignment.collector.user,
                kind="photo",
                file=rel_path,
                original_name=doc_meta["original_name"],
                mime_type=doc_meta["mime_type"],
                size_bytes=doc_meta["size_bytes"],
                sha256=doc_meta["sha256"],
            )
            append_audit_event(
                actor=assignment.collector.user,
                action="document_uploaded",
                entity_type="Document",
                entity_id=activity.pk,
                account=account,
                changes={"kind": "photo", "size_bytes": doc_meta["size_bytes"]},
            )

        _audit(assignment, activity, "visit_submitted")
        notify_client_reviewers(account, "activities.change_fieldactivity", f"visit-review-needed-{activity.pk}",
            f"Visit report for account {account.reference} is awaiting review.", exclude_user_id=user_id)
        if normalized["follow_up"]:
            notify_client_reviewers(account, "activities.change_followuptask", f"followup-request-{task.pk}",
                f"Follow-up request for account {account.reference} is awaiting scheduling.", exclude_user_id=user_id)

        return {"saved": True, "created": True, "activity_id": activity.pk, "visit": activity_payload(activity), "gate": visit_gate(assignment, user_id, now)}

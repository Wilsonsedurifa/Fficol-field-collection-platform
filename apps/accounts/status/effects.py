"""Orchestration of side effects for account status transitions.

Calls small, dedicated mutation helpers in the respective owner apps
(assignments, activities, collection) under the caller's transaction and lock context.
"""

from apps.activities.services import (
    cancel_in_progress_visit,
    cancel_pending_followups,
)
from apps.assignments.services import (
    complete_or_release_assignment,
    supersede_release_requests,
    withdraw_pending_offers,
)
from apps.collection.services import cancel_open_promises

from .constants import PAUSED_STATUSES, TERMINAL_STATUSES


def apply_status_transition_side_effects(account, assignment, to_status, reason, reviewer, now):
    """Execute side effects for transitioning an account to a target status.

    - Terminal targets (resolved, closed, invalid):
      * Active assignment -> status "completed", ended_at=now, end_reason
      * Pending offers -> "withdrawn"
      * FollowUpTask requested/pending -> "cancelled"
      * PromiseToPay open -> "cancelled"
      * AssignmentReleaseRequest requested -> "superseded"
      * In-progress FieldActivity -> "cancelled"
    - Paused targets (on_hold, disputed, legal):
      * Active assignment -> status "released", ended_at=now, end_reason
      * Pending offers -> "withdrawn"
      * FollowUpTask requested/pending -> "cancelled"
      * AssignmentReleaseRequest requested -> "superseded"
      * In-progress FieldActivity -> "cancelled"
    - Reopen (open):
      * No side effects; only account status changes.
    """
    if to_status not in TERMINAL_STATUSES and to_status not in PAUSED_STATUSES:
        return {}

    is_terminal = to_status in TERMINAL_STATUSES
    effects = {}

    if assignment is not None and assignment.status == "active":
        effects["assignment"] = complete_or_release_assignment(
            assignment=assignment,
            to_status=to_status,
            reason=reason,
            reviewer=reviewer,
            now=now,
            is_terminal=is_terminal,
        )

    effects["withdrawn_offers_count"] = withdraw_pending_offers(
        account=account,
        reviewer=reviewer,
    )

    effects["cancelled_followups_count"] = cancel_pending_followups(
        account_or_assignment=assignment if assignment is not None else account,
        reviewer=reviewer,
        reason=reason,
        now=now,
    )

    effects["superseded_release_requests_count"] = supersede_release_requests(
        account_or_assignment=assignment if assignment is not None else account,
        reviewer=reviewer,
        reason=reason,
        now=now,
    )

    effects["cancelled_visits_count"] = cancel_in_progress_visit(
        account_or_assignment=assignment if assignment is not None else account,
        reviewer=reviewer,
        now=now,
    )

    effects["cancelled_promises_count"] = cancel_open_promises(
        account=account,
        reviewer=reviewer,
        now=now,
    )

    return effects

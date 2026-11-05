"""Constants for account status transitions, lifecycle groups, and error codes."""

from apps.accounts.models import Account

TERMINAL_STATUSES = frozenset({
    Account.Status.RESOLVED,
    Account.Status.CLOSED,
    Account.Status.INVALID,
})

PAUSED_STATUSES = frozenset({
    Account.Status.ON_HOLD,
    Account.Status.DISPUTED,
    Account.Status.LEGAL,
})

ALLOWED_STATUS_TRANSITIONS = {
    Account.Status.OPEN: frozenset({
        Account.Status.ON_HOLD,
        Account.Status.DISPUTED,
        Account.Status.LEGAL,
        Account.Status.RESOLVED,
        Account.Status.CLOSED,
        Account.Status.INVALID,
    }),
    Account.Status.ON_HOLD: frozenset({
        Account.Status.OPEN,
        Account.Status.CLOSED,
    }),
    Account.Status.DISPUTED: frozenset({
        Account.Status.OPEN,
        Account.Status.LEGAL,
        Account.Status.CLOSED,
    }),
    Account.Status.LEGAL: frozenset({
        Account.Status.OPEN,
        Account.Status.CLOSED,
    }),
    Account.Status.RESOLVED: frozenset({
        Account.Status.CLOSED,
        Account.Status.OPEN,
    }),
    Account.Status.CLOSED: frozenset({
        Account.Status.OPEN,
    }),
    Account.Status.INVALID: frozenset({
        Account.Status.OPEN,
    }),
}

# Error codes
INVALID_STATUS_TRANSITION = "invalid_status_transition"
REASON_REQUIRED = "reason_required"
CONFIRMATION_REQUIRED = "confirmation_required"
UNVERIFIED_PAYMENTS_EXIST = "unverified_payments_exist"
ZERO_BALANCE_REQUIRED = "zero_balance_required"
PERMISSION_REQUIRED = "permission_required"
CLIENT_ACCESS_REQUIRED = "client_access_required"
EVENT_CONFLICT = "event_conflict"
ACCOUNT_NOT_FOUND = "account_not_found"
BUSY = "busy"

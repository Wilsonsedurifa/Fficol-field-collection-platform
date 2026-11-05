"""Guard helpers for account lifecycle and collector operations."""

from apps.accounts.models import Account


def check_account_is_open(account, error_cls=None):
    """Enforce that an account must be in 'open' status for collector field operations.

    Raises the specified error_cls (or ValueError) with code 'account_not_open'
    and HTTP 409 status if the account is not open.
    """
    if account.status != Account.Status.OPEN:
        message = f"This account is not open for field collection. Current status: {account.status}."
        if error_cls is not None:
            raise error_cls("account_not_open", message, 409)
        raise ValueError(message)

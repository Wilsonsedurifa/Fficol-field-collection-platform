"""Shared account checks; authorization belongs to the calling workflow."""

from django.core.exceptions import ValidationError

from apps.collection.services import validate_commission_terms

from .guards import check_account_is_open
from .models import Account, CustomerAddress


def validate_account_for_offers(account):
    """Validate the account foundation, not collector eligibility or ownership."""
    account.full_clean()
    if not account.client.is_active or account.status != Account.Status.OPEN:
        raise ValidationError("Only open accounts from active clients can receive offers.")
    address = account.collection_address
    if address is None or address.position is None:
        raise ValidationError("A collection address with known coordinates is required.")
    address.full_clean()
    if address.verification_status != CustomerAddress.VerificationStatus.VERIFIED:
        raise ValidationError("The collection address must be verified before matching.")
    if account.commission_rule is None or not account.commission_rule.is_active:
        raise ValidationError("Active earning terms are required before offering an account.")
    validate_commission_terms(account.commission_rule)
    return account

"""Forms for Back Office account status transitions."""

from django import forms
from django.core.exceptions import ValidationError

from apps.accounts.models import Account
from .constants import ALLOWED_STATUS_TRANSITIONS, TERMINAL_STATUSES


class ChangeAccountStatusForm(forms.Form):
    to_status = forms.ChoiceField(
        choices=Account.Status.choices,
        widget=forms.Select(attrs={"class": "form-select", "id": "id_to_status"}),
        label="New Status",
    )
    reason = forms.CharField(
        widget=forms.Textarea(attrs={
            "class": "form-control",
            "rows": 3,
            "maxlength": 5000,
            "placeholder": "Explain the reason for changing the account status...",
            "id": "id_status_reason",
        }),
        max_length=5000,
        required=True,
        label="Reason for status change",
    )
    confirm_terminal = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input", "id": "id_confirm_terminal"}),
        label="I confirm the source balance snapshot is zero and payment reports are verified.",
    )
    event_id = forms.UUIDField(
        widget=forms.HiddenInput(attrs={"id": "id_status_event_id"}),
    )

    def __init__(self, *args, current_status=None, **kwargs):
        super().__init__(*args, **kwargs)
        if current_status:
            allowed = ALLOWED_STATUS_TRANSITIONS.get(current_status, frozenset())
            self.fields["to_status"].choices = [
                (val, label) for val, label in Account.Status.choices if val in allowed
            ]

    def clean_reason(self):
        reason = self.cleaned_data.get("reason", "").strip()
        if not reason:
            raise ValidationError("Provide a reason for the status change.")
        return reason

    def clean(self):
        cleaned_data = super().clean()
        to_status = cleaned_data.get("to_status")
        confirm_terminal = cleaned_data.get("confirm_terminal")
        if to_status in TERMINAL_STATUSES and not confirm_terminal:
            self.add_error("confirm_terminal", "You must confirm this terminal status change.")
        return cleaned_data

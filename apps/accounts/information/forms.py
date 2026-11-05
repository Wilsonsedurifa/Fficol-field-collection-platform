"""Collector information form with server-side category validation."""

from django import forms

from .models import AccountInformationProposal as Proposal
from .services import InformationWorkflowError, normalized_proposed_payload
from ..models import CustomerAddress


class InformationProposalForm(forms.Form):
    event_id = forms.UUIDField(widget=forms.HiddenInput)
    category = forms.ChoiceField(choices=Proposal.Category.choices, widget=forms.HiddenInput)
    value = forms.CharField(label="Proposed value", max_length=500, required=False)
    address_kind = forms.ChoiceField(label="Address type", choices=CustomerAddress.Kind.choices, required=False)
    address_line = forms.CharField(label="Proposed address", max_length=300, required=False, widget=forms.Textarea(attrs={"rows": 3}))
    latitude = forms.FloatField(min_value=-90, max_value=90, required=False)
    longitude = forms.FloatField(min_value=-180, max_value=180, required=False)
    description = forms.CharField(label="Proposed payment arrangement", max_length=5000, required=False, widget=forms.Textarea(attrs={"rows": 4}))
    explanation = forms.CharField(label="How did you confirm this information?", max_length=5000, widget=forms.Textarea(attrs={"rows": 3}))
    evidence_id = forms.TypedChoiceField(label="Existing supporting document (optional)", coerce=int, empty_value=None, required=False)
    evidence_reference = forms.CharField(label="Supporting reference (optional)", max_length=500, required=False,
        help_text="Describe where the information came from. Do not enter passwords or access codes.")

    def __init__(self, *args, category="email", documents=(), **kwargs):
        super().__init__(*args, **kwargs)
        selected = self.data.get("category") if self.is_bound else category
        groups = {"address": {"address_kind", "address_line", "latitude", "longitude"},
                  "payment_arrangement": {"description"}, "email": {"value"}, "phone": {"value"}, "contact_person": {"value"}}
        optional_fields = {"value", "address_kind", "address_line", "latitude", "longitude", "description"}
        for field in optional_fields - groups.get(selected, set()):
            del self.fields[field]
        self.initial.setdefault("category", selected)
        if "value" in self.fields:
            self.fields["value"].label = {"email": "Proposed email", "phone": "Proposed phone number", "contact_person": "Contact person's name"}[selected]
            self.fields["value"].required = True
            if selected == "email":
                self.fields["value"].widget = forms.EmailInput()
            elif selected == "phone":
                self.fields["value"].widget = forms.TextInput(attrs={"inputmode": "tel"})
        for field in ("address_kind", "address_line", "description"):
            if field in self.fields:
                self.fields[field].required = True
        self.fields["evidence_id"].choices = [("", "No document selected")] + [(doc.pk, doc.original_name) for doc in documents]
        if "latitude" in self.fields:
            self.fields["latitude"].help_text = "Optional. Provide latitude and longitude together; leave both blank if unknown."

    def clean(self):
        cleaned = super().clean()
        category = cleaned.get("category")
        if self.errors or category is None:
            return cleaned
        if category == "address":
            payload = {"kind": cleaned["address_kind"], "address_line": cleaned["address_line"], "latitude": cleaned["latitude"], "longitude": cleaned["longitude"]}
        elif category == "payment_arrangement":
            payload = {"description": cleaned["description"]}
        else:
            payload = {"value": cleaned["value"]}
        try:
            cleaned["proposed_payload"] = normalized_proposed_payload(category, payload)
        except InformationWorkflowError as exc:
            raise forms.ValidationError(exc.message) from exc
        return cleaned

    def submission_data(self):
        return {field: self.cleaned_data[field] for field in ("event_id", "category", "proposed_payload", "explanation", "evidence_id", "evidence_reference")}


class InformationDecisionForm(forms.Form):
    decision = forms.ChoiceField(choices=[("", "Choose a decision"), ("approve", "Approve"), ("reject", "Reject")])
    reason = forms.CharField(label="Review note", max_length=5000, required=False,
        widget=forms.Textarea(attrs={"rows": 3}), help_text="Required when rejecting a proposal.")

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("decision") == "reject" and not cleaned.get("reason", "").strip():
            self.add_error("reason", "Explain why this proposal is being rejected.")
        return cleaned

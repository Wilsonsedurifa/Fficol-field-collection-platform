"""Collector information forms and protected endpoints."""

import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client as TestClient, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.information.forms import InformationProposalForm
from apps.accounts.information.models import AccountInformationProposal as Proposal, CustomerContact
from .test_services import information_fixture
from apps.activities.models import Document
from apps.assignments.models import AccountAssignment
from apps.audit.models import AuditEvent
from apps.user.models import ClientAccess, CollectorProfile


class InformationFormTests(SimpleTestCase):
    def data(self, **overrides):
        data = {"event_id": str(uuid.uuid4()), "category": "email", "value": "new@example.test", "explanation": "Customer confirmed"}
        data.update(overrides)
        return data

    def test_each_category_has_server_validated_payload(self):
        cases = [({"category": "email", "value": "new@EXAMPLE.TEST"}, {"value": "new@example.test"}),
                 ({"category": "phone", "value": "+63 (917) 123-4567"}, {"value": "+639171234567"}),
                 ({"category": "contact_person", "value": "Maria Sample"}, {"value": "Maria Sample"}),
                 ({"category": "address", "address_kind": "residential", "address_line": "Test street"}, {"kind": "residential", "address_line": "Test street", "latitude": None, "longitude": None}),
                 ({"category": "payment_arrangement", "description": "Discuss monthly payments"}, {"description": "Discuss monthly payments"})]
        for values, expected in cases:
            with self.subTest(category=values["category"]):
                form = InformationProposalForm(self.data(**values))
                self.assertTrue(form.is_valid(), form.errors)
                self.assertEqual(form.submission_data()["proposed_payload"], expected)

    def test_invalid_and_missing_values_are_rejected(self):
        cases = [{"value": "bad email"}, {"category": "phone", "value": "letters"},
                 {"category": "contact_person", "value": " "}, {"category": "unknown"},
                 {"category": "address", "address_kind": "residential", "address_line": "Test", "latitude": "14"},
                 {"category": "address", "address_kind": "residential", "address_line": "Test", "latitude": "nan", "longitude": "120"},
                 {"category": "payment_arrangement", "description": ""}, {"explanation": ""}, {"event_id": "bad"}, {"evidence_id": "999"}]
        for values in cases:
            with self.subTest(values=values):
                self.assertFalse(InformationProposalForm(self.data(**values)).is_valid())

    def test_form_only_renders_fields_for_chosen_category(self):
        form = InformationProposalForm(category="email")
        self.assertIn("value", form.fields)
        self.assertNotIn("address_line", form.fields)
        self.assertNotIn("description", form.fields)


class CollectorInformationEndpointTests(TestCase):
    def setUp(self):
        self.collector, self.reviewer, self.account, self.assignment = information_fixture()
        self.url = reverse("assignments:propose_information", args=[self.assignment.pk])
        self.client.force_login(self.collector)

    def data(self, **overrides):
        data = {"event_id": str(uuid.uuid4()), "category": "email", "value": "new@example.test", "explanation": "Customer confirmed"}
        data.update(overrides)
        return data

    def assert_denied(self, expected=(403, 404)):
        for method in ("get", "post"):
            response = getattr(self.client, method)(self.url, self.data() if method == "post" else {})
            self.assertIn(response.status_code, expected)
        self.assertFalse(Proposal.objects.exists())

    def test_url_page_and_account_entry_card(self):
        self.assignment.policy_snapshot = {"commission_terms": {"kind": "percentage", "percentage": "10", "earning_basis": "verified_payment"}}
        self.assignment.save(update_fields=["policy_snapshot"])
        self.assertEqual(self.url, f"/collector/accounts/{self.assignment.pk}/information/propose/")
        response = self.client.get(self.url)
        self.assertContains(response, "No information proposals submitted")
        self.assertContains(response, "csrfmiddlewaretoken")
        self.assertContains(response, "css/information/collector.css")
        self.assertContains(self.client.get(reverse("assignments:account_detail", args=[self.assignment.pk])), self.url)

    def test_submit_exact_retry_and_changed_retry(self):
        data = self.data()
        self.assertRedirects(self.client.post(self.url, data), self.url)
        self.assertRedirects(self.client.post(self.url, data), self.url)
        self.assertEqual(Proposal.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.count(), 1)
        data["explanation"] = "Changed explanation"
        self.assertEqual(self.client.post(self.url, data).status_code, 409)
        self.assertEqual(AuditEvent.objects.count(), 1)
        self.assertFalse(CustomerContact.objects.exists())

    def test_invalid_post_preserves_input_and_token(self):
        data = self.data(value="invalid")
        response = self.client.post(self.url, data)
        self.assertContains(response, data["event_id"], status_code=400)
        self.assertContains(response, 'value="invalid"', status_code=400)
        self.assertFalse(Proposal.objects.exists())

    def test_anonymous_get_and_post_redirect_to_login(self):
        self.client.logout()
        self.assert_denied(expected=(302,))

    def test_inactive_user_is_denied(self):
        get_user_model().objects.filter(pk=self.collector.pk).update(is_active=False)
        self.assert_denied(expected=(302,))

    def test_unapproved_collector_is_denied(self):
        CollectorProfile.objects.filter(user=self.collector).update(approval_status="pending")
        self.assert_denied()

    def test_backoffice_user_is_denied_even_with_profile(self):
        permission = Permission.objects.get(content_type__app_label="accounts", codename="validate_account_information")
        self.collector.user_permissions.add(permission)
        self.assert_denied()

    def test_wrong_client_is_denied(self):
        ClientAccess.objects.filter(user=self.collector).update(is_active=False)
        self.assert_denied()

    def test_non_owner_is_denied(self):
        other = get_user_model().objects.create_user(username="information-nonowner")
        CollectorProfile.objects.create(user=other, approval_status="approved", approved_by=self.reviewer, approved_at=timezone.now())
        ClientAccess.objects.create(user=other, client=self.account.client)
        self.client.force_login(other)
        self.assert_denied()

    def test_former_owner_denied_after_release_or_reassignment(self):
        for status in ("released", "reassigned"):
            AccountAssignment.objects.filter(pk=self.assignment.pk).update(status=status, ended_at=timezone.now())
            self.assert_denied()

    def test_closed_account_is_denied(self):
        self.account.status = "closed"
        self.account.save(update_fields=["status"])
        self.assert_denied(expected=(409,))

    def test_csrf_is_required(self):
        client = TestClient(enforce_csrf_checks=True)
        client.force_login(self.collector)
        self.assertEqual(client.post(self.url, self.data()).status_code, 403)
        client.get(self.url)
        data = self.data(csrfmiddlewaretoken=client.cookies["csrftoken"].value)
        self.assertEqual(client.post(self.url, data).status_code, 302)

    def test_all_categories_render_and_payment_copy(self):
        for category in Proposal.Category.values:
            self.assertEqual(self.client.get(self.url, {"category": category}).status_code, 200)
        self.assertContains(self.client.get(self.url, {"category": "payment_arrangement"}), "does not verify payment")

    def test_history_escapes_text_and_excludes_former_assignment(self):
        self.client.post(self.url, self.data(explanation="<script>alert('private')</script>"))
        response = self.client.get(self.url)
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>alert('private')</script>")
        AccountAssignment.objects.filter(pk=self.assignment.pk).update(status="released", ended_at=timezone.now())
        new_assignment = AccountAssignment.objects.create(account=self.account, collector=self.assignment.collector,
            target_address=self.assignment.target_address, commission_rule=self.assignment.commission_rule,
            target_position=self.assignment.target_position, policy_snapshot={"test": True}, first_action_due_at=timezone.now() + timedelta(days=1))
        response = self.client.get(reverse("assignments:propose_information", args=[new_assignment.pk]))
        self.assertContains(response, "No information proposals submitted")
        self.assertNotContains(response, "alert(&#x27;private&#x27;)")

    def test_only_account_evidence_can_be_selected(self):
        from apps.accounts.models import Account
        other = Account.objects.create(client=self.account.client, customer=self.account.customer, source_system="TEST", external_id="OTHER", reference="OTHER", balance_snapshot=1, balance_as_of=timezone.now())
        document = Document.objects.create(account=other, uploaded_by=self.collector, kind="photo", file="never-opened.jpg", original_name="other-private.jpg", mime_type="image/jpeg", size_bytes=12, sha256="a" * 64)
        self.assertNotContains(self.client.get(self.url), "other-private.jpg")
        self.assertEqual(self.client.post(self.url, self.data(evidence_id=document.pk)).status_code, 400)
        self.assertFalse(Proposal.objects.exists())

    def test_history_pagination(self):
        for index in range(11):
            self.client.post(self.url, self.data(value=f"person{index}@example.test"))
        response = self.client.get(self.url)
        self.assertEqual(len(response.context["proposals"]), 10)
        self.assertContains(response, "Page 1 of 2")
        self.assertEqual(len(self.client.get(self.url, {"page": 2}).context["proposals"]), 1)

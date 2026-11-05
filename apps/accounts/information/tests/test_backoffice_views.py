"""Information-validation role classification and workflow isolation."""

import uuid

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import Client as TestClient, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.information.services import can_validate_information
from apps.accounts.information.services import submit_proposal
from apps.accounts.information.models import AccountInformationProposal as Proposal, CustomerContact
from apps.accounts.models import Account, Client as CreditorClient, Customer
from apps.activities.models import Document
from apps.assignments.models import AccountAssignment
from apps.audit.models import AuditEvent
from apps.collection.models import Payment, PromiseToPay
from apps.user.models import CollectorProfile, has_backoffice_access
from apps.user.models import ClientAccess
from .test_services import information_fixture


class InformationPermissionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="validation-only")
        self.permission = Permission.objects.get(content_type__app_label="accounts", codename="validate_account_information")
        self.user.user_permissions.add(self.permission)
        self.client.force_login(self.user)

    def test_permission_label_and_direct_role_classification(self):
        self.assertEqual(self.permission.name, "Can validate account information proposals")
        self.assertTrue(can_validate_information(self.user))
        self.assertTrue(has_backoffice_access(self.user))
        self.assertFalse(self.user.is_staff)

    def test_validation_only_login_and_dashboard(self):
        self.assertRedirects(self.client.get(reverse("login_success")), reverse("supervisor_dashboard"))
        response = self.client.get(reverse("supervisor_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["can_validate_information"])
        self.assertContains(response, reverse("backoffice_information_proposals"))
        for key in ("can_review", "can_schedule", "can_verify_payments", "can_review_releases", "can_manage_assignments"):
            self.assertFalse(response.context[key])
        for label in ("Visit Reviews", "Follow-up Scheduling", "Payment Verifications", "Release Requests", "Account Assignments"):
            self.assertNotContains(response, label)

    def test_other_workflow_pages_are_denied(self):
        for route in ("backoffice_visits", "backoffice_followups", "backoffice_payments", "backoffice_release_requests", "backoffice_assignments"):
            with self.subTest(route=route):
                self.assertEqual(self.client.get(reverse(route)).status_code, 403)

    def test_no_other_workflow_permission_granted(self):
        for permission in ("activities.change_fieldactivity", "activities.change_followuptask", "collection.change_payment", "assignments.review_release_requests", "assignments.manage_account_assignments"):
            self.assertFalse(self.user.has_perm(permission))

    def test_retained_collector_profile_does_not_enable_collector_pages(self):
        CollectorProfile.objects.create(user=self.user, approval_status="approved", approved_by=self.user, approved_at=timezone.now())
        for route in ("collector_dashboard", "assignments:account_offers", "assignments:my_accounts", "activities:followups"):
            self.assertEqual(self.client.get(reverse(route)).status_code, 403)

    def test_group_permission_also_classifies_as_backoffice(self):
        self.user.user_permissions.clear()
        group = Group.objects.create(name="Information validators")
        group.permissions.add(self.permission)
        self.user.groups.add(group)
        fresh = get_user_model().objects.get(pk=self.user.pk)
        self.assertTrue(has_backoffice_access(fresh))
        self.assertTrue(can_validate_information(fresh))

    def test_inactive_validator_is_denied(self):
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        self.assertFalse(has_backoffice_access(self.user))
        self.assertFalse(can_validate_information(self.user))
        response = self.client.get(reverse("supervisor_dashboard"))
        self.assertEqual(response.status_code, 302)


class InformationBackofficeQueueTests(TestCase):
    def setUp(self):
        self.collector, self.reviewer, self.account, self.assignment = information_fixture()
        self.reviewer.is_superuser = False
        self.reviewer.is_staff = False
        self.reviewer.save(update_fields=["is_superuser", "is_staff"])
        self.reviewer.user_permissions.add(Permission.objects.get(content_type__app_label="accounts", codename="validate_account_information"))
        self.client.force_login(self.reviewer)
        self.url = reverse("backoffice_information_proposals")

    def proposal(self, **overrides):
        data = dict(event_id=uuid.uuid4(), category="email", proposed_payload={"value": "private@example.test"}, explanation="Customer confirmed")
        data.update(overrides)
        return submit_proposal(self.collector, self.assignment.pk, **data)["proposal"]

    def decision_url(self, proposal):
        return reverse("backoffice_information_decision", args=[proposal.pk])

    def decide(self, proposal, decision="approve", reason="", **overrides):
        data = {f"proposal-{proposal.pk}-decision": decision, f"proposal-{proposal.pk}-reason": reason}
        data.update(overrides)
        return self.client.post(self.decision_url(proposal), data)

    def test_paths_and_collector_reverse_are_preserved(self):
        proposal = self.proposal()
        self.assertEqual(self.url, "/back-office/information-proposals/")
        self.assertEqual(self.decision_url(proposal), f"/back-office/information-proposals/{proposal.pk}/decision/")
        self.assertEqual(reverse("assignments:propose_information", args=[self.assignment.pk]), f"/collector/accounts/{self.assignment.pk}/information/propose/")

    def test_empty_queue_and_styles(self):
        response = self.client.get(self.url)
        self.assertContains(response, "No matching proposals")
        self.assertContains(response, "css/information/backoffice.css")
        self.assertContains(response, "css/backoffice/portal.css")
        self.assertContains(response, "Information Validation")
        self.assertNotContains(response, 'style="')

    def test_validation_only_reviewer_sees_values_provenance_and_decides(self):
        proposal = self.proposal()
        response = self.client.get(self.url)
        self.assertContains(response, "private@example.test")
        self.assertContains(response, self.collector.username)
        self.assertContains(response, "Recorded when submitted")
        self.assertContains(response, "Proposed information")
        self.assertContains(response, "Submitting assignment")
        self.assertEqual(self.decide(proposal).status_code, 302)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, "approved")
        self.assertEqual(proposal.reviewed_by_id, self.reviewer.pk)
        self.assertIsNotNone(proposal.reviewed_at)
        self.assertEqual(CustomerContact.objects.get().validated_from_proposal_id, proposal.pk)
        self.assertEqual(AuditEvent.objects.latest("pk").action, "information_proposal_approved")

    def test_reject_requires_reason_and_preserves_bound_form(self):
        proposal = self.proposal()
        response = self.decide(proposal, "reject", "  ")
        self.assertContains(response, "Explain why", status_code=400)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, "pending")
        self.assertFalse(CustomerContact.objects.exists())
        self.assertEqual(self.decide(proposal, "reject", "Unable to confirm").status_code, 302)
        proposal.refresh_from_db()
        self.assertEqual(proposal.review_reason, "Unable to confirm")
        self.assertEqual(AuditEvent.objects.latest("pk").action, "information_proposal_rejected")
        self.assertFalse(CustomerContact.objects.exists())

    def test_finalized_decision_returns_conflict_without_duplicate_audit(self):
        proposal = self.proposal()
        self.decide(proposal)
        response = self.decide(proposal, "reject", "Changed mind")
        self.assertContains(response, "already been reviewed", status_code=409)
        self.assertEqual(AuditEvent.objects.count(), 2)
        self.assertEqual(CustomerContact.objects.count(), 1)

    def test_filters_search_and_pagination(self):
        for index in range(11):
            self.proposal(proposed_payload={"value": f"person{index}@example.test"})
        phone = self.proposal(category="phone", proposed_payload={"value": "+639171234567"})
        response = self.client.get(self.url)
        self.assertEqual(len(response.context["rows"]), 10)
        self.assertContains(response, "Page 1 of 2")
        for query in (self.account.reference, self.account.customer.display_name, self.collector.username):
            self.assertEqual(self.client.get(self.url, {"q": query}).context["page_obj"].paginator.count, 12)
        self.assertEqual(self.client.get(self.url, {"q": "not found"}).context["page_obj"].paginator.count, 0)
        self.assertEqual(self.client.get(self.url, {"category": "phone"}).context["page_obj"].paginator.count, 1)
        self.decide(phone, "reject", "Wrong number")
        self.assertEqual(self.client.get(self.url, {"status": "rejected"}).context["page_obj"].paginator.count, 1)
        response = self.client.get(self.url, {"status": "all", "page": 2, "category": "all", "q": self.account.reference})
        self.assertEqual(len(response.context["rows"]), 2)
        self.assertContains(response, "status=all&amp;category=all&amp;q=INFO-1")

    def test_other_client_proposal_never_appears_and_cannot_be_reviewed(self):
        other_client = CreditorClient.objects.create(code="PRIVATE", name="Other client")
        customer = Customer.objects.create(client=other_client, source_system="TEST", display_name="Hidden customer")
        account = Account.objects.create(client=other_client, customer=customer, source_system="TEST", external_id="HIDDEN", reference="HIDDEN", balance_snapshot=1, balance_as_of=timezone.now())
        proposal = self.proposal()
        # Simulates another client's historical proposal without changing the collector's grants.
        Proposal.objects.filter(pk=proposal.pk).update(account=account, customer=customer)
        self.assertNotContains(self.client.get(self.url), "HIDDEN")
        self.assertEqual(self.decide(proposal).status_code, 404)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, "pending")

    def test_revoked_scope_denies_review_and_hides_queue(self):
        proposal = self.proposal()
        ClientAccess.objects.filter(user=self.reviewer).update(is_active=False)
        self.assertContains(self.client.get(self.url), "No matching proposals")
        self.assertEqual(self.decide(proposal).status_code, 404)

    def test_self_review_is_hidden_and_denied_server_side(self):
        proposal = self.proposal()
        self.collector.user_permissions.add(Permission.objects.get(content_type__app_label="accounts", codename="validate_account_information"))
        self.client.force_login(self.collector)
        self.assertContains(self.client.get(self.url), "You cannot review your own submission")
        self.assertEqual(self.decide(proposal).status_code, 403)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, "pending")

    def test_collector_and_wrong_role_reviewer_are_denied(self):
        proposal = self.proposal()
        self.client.force_login(self.collector)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.decide(proposal).status_code, 403)
        self.reviewer.user_permissions.clear()
        self.reviewer.user_permissions.add(Permission.objects.get(content_type__app_label="collection", codename="change_payment"))
        self.client.force_login(self.reviewer)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.decide(proposal).status_code, 403)

    def test_anonymous_and_inactive_are_denied(self):
        proposal = self.proposal()
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.assertEqual(self.decide(proposal).status_code, 302)
        get_user_model().objects.filter(pk=self.reviewer.pk).update(is_active=False)
        self.client.force_login(self.reviewer)
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_decision_requires_post_and_csrf(self):
        proposal = self.proposal()
        self.assertEqual(self.client.get(self.decision_url(proposal)).status_code, 405)
        self.assertEqual(self.client.post(self.url).status_code, 405)
        client = TestClient(enforce_csrf_checks=True)
        client.force_login(self.reviewer)
        data = {f"proposal-{proposal.pk}-decision": "approve"}
        self.assertEqual(client.post(self.decision_url(proposal), data).status_code, 403)
        client.get(self.url)
        data["csrfmiddlewaretoken"] = client.cookies["csrftoken"].value
        self.assertEqual(client.post(self.decision_url(proposal), data).status_code, 302)

    def test_pending_proposal_review_after_release_preserves_assignment(self):
        proposal = self.proposal()
        AccountAssignment.objects.filter(pk=self.assignment.pk).update(status="released", ended_at=timezone.now())
        before = AccountAssignment.objects.values().get(pk=self.assignment.pk)
        self.assertEqual(self.decide(proposal).status_code, 302)
        self.assertEqual(AccountAssignment.objects.values().get(pk=self.assignment.pk), before)
        self.client.force_login(self.collector)
        url = reverse("assignments:propose_information", args=[self.assignment.pk])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_stale_value_conflict_is_explained(self):
        first = self.proposal()
        second = self.proposal(proposed_payload={"value": "other@example.test"})
        self.decide(first)
        self.assertContains(self.client.get(self.url), "Current information has changed")
        self.assertContains(self.decide(second), "Current information has changed", status_code=409)
        second.refresh_from_db()
        self.assertEqual(second.status, "pending")

    def test_payment_arrangement_copy_and_no_financial_side_effects(self):
        proposal = self.proposal(category="payment_arrangement", proposed_payload={"description": "Customer proposes monthly payments"})
        self.assertContains(self.client.get(self.url), "does not verify payment, update the balance")
        account_before = Account.objects.values().get(pk=self.account.pk)
        assignment_before = AccountAssignment.objects.values().get(pk=self.assignment.pk)
        self.assertEqual(self.decide(proposal).status_code, 302)
        self.assertEqual(Account.objects.values().get(pk=self.account.pk), account_before)
        self.assertEqual(AccountAssignment.objects.values().get(pk=self.assignment.pk), assignment_before)
        self.assertFalse(Payment.objects.exists())
        self.assertFalse(PromiseToPay.objects.exists())

    def test_evidence_link_uses_existing_private_download_and_is_lost_after_release(self):
        document = Document.objects.create(account=self.account, uploaded_by=self.collector, kind="photo", file="never-opened.jpg", original_name="private-proof.jpg", mime_type="image/jpeg", size_bytes=12, sha256="a" * 64)
        self.proposal(evidence_id=document.pk)
        self.assertContains(self.client.get(self.url), reverse("download_document", args=[document.pk]))
        AccountAssignment.objects.filter(pk=self.assignment.pk).update(status="released", ended_at=timezone.now())
        self.client.force_login(self.collector)
        self.assertEqual(self.client.get(reverse("download_document", args=[document.pk])).status_code, 404)

    def test_submitted_text_is_escaped(self):
        self.proposal(explanation="<script>alert('test')</script>")
        response = self.client.get(self.url)
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>alert('test')</script>")

    def test_redirect_only_uses_approved_local_destination_and_preserves_filters(self):
        proposal = self.proposal()
        response = self.decide(proposal, status="all", category="email", q="INFO-1", page="2", next="https://external.invalid/")
        self.assertEqual(response.url, self.url + "?status=all&category=email&q=INFO-1&page=2")

    def test_collector_rejection_corrected_submission_approval_and_access_revocation(self):
        collector_client = TestClient(enforce_csrf_checks=True)
        collector_client.force_login(self.collector)
        collector_url = reverse("assignments:propose_information", args=[self.assignment.pk])
        collector_client.get(collector_url)
        submission = {"csrfmiddlewaretoken": collector_client.cookies["csrftoken"].value,
            "event_id": str(uuid.uuid4()), "category": "email", "value": "first@example.test", "explanation": "Confirmed during visit"}
        self.assertEqual(collector_client.post(collector_url, submission).status_code, 302)
        original = Proposal.objects.get()
        self.assertEqual(self.decide(original, "reject", "Please confirm the corrected spelling").status_code, 302)
        self.assertContains(collector_client.get(collector_url), "Please confirm the corrected spelling")
        self.assertFalse(CustomerContact.objects.exists())
        submission.update(event_id=str(uuid.uuid4()), value="corrected@example.test", explanation="Customer confirmed corrected spelling")
        self.assertEqual(collector_client.post(collector_url, submission).status_code, 302)
        corrected = Proposal.objects.latest("pk")
        self.assertEqual(self.decide(corrected).status_code, 302)
        self.assertContains(collector_client.get(collector_url), "Approved")
        self.assertEqual(CustomerContact.objects.get().value, "corrected@example.test")
        original.refresh_from_db()
        self.assertEqual(original.status, "rejected")
        self.assertEqual(AuditEvent.objects.count(), 4)
        AccountAssignment.objects.filter(pk=self.assignment.pk).update(status="released", ended_at=timezone.now())
        self.assertEqual(collector_client.get(collector_url).status_code, 404)
        self.assertEqual(collector_client.post(collector_url, submission).status_code, 404)
        self.assertContains(self.client.get(self.url, {"status": "all"}), "corrected@example.test")

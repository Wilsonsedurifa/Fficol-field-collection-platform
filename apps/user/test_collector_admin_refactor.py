"""Tests for the System Admin Collector list and Collector edit refactor.

Covers:
- Three-dot overflow menu displays context-aware actions for Active, Suspended, Offboarded, and Superuser accounts.
- Complete removal of administrator-entered password fields from all Collector admin list, create, and edit pages.
- Explicit lifecycle actions: Suspend (requires reason), Reinstate (optional note), Offboard (permanent read-only), Warnings (warning, final_warning).
- Safe password/invitation email actions: Resend Setup Invitation (incomplete setup), Send Password Reset Email (completed setup).
- Audit log entries recorded for all lifecycle and email actions.
- Offboarded collector enforces read-only mode and rejects edits.
- Collector detail page separates the 6 distinct concerns.
"""

from django.core.exceptions import ValidationError
from django.core import mail
from django.test import TestCase, Client as HttpClient
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Client
from apps.audit.models import AuditEvent
from apps.user.models import (
    User,
    CollectorProfile,
    CollectorInvitation,
    CollectorDisciplinaryRecord,
    ClientAccess,
)
from apps.user.admin_portal.services import (
    create_collector_account,
    update_collector_account,
    suspend_collector_profile,
    reinstate_collector_profile,
    offboard_collector_profile,
    issue_collector_warning,
    send_collector_password_reset_email,
    approve_collector_profile,
    resend_collector_invitation,
    accept_collector_invitation,
)
from apps.user.admin_portal.forms import CollectorEditForm, CollectorCreateForm


class CollectorAdminRefactorTests(TestCase):
    def setUp(self):
        self.client = HttpClient()
        # System Admin user
        self.admin = User.objects.create_superuser(
            username="admin_sys",
            password="AdminPassword123!",
            email="admin@ficol.test",
        )
        self.client.force_login(self.admin)

        # Active Client for scoping tests
        self.test_client = Client.objects.create(
            name="Alpha Corp",
            code="ALPHA",
            is_active=True,
        )

    def _create_active_collector(self, username="active_col"):
        """Helper to create an approved, setup-complete active collector."""
        user = create_collector_account(
            actor=self.admin,
            username=username,
            email=f"{username}@ficol.test",
            first_name="Active",
            last_name="Collector",
        )
        # Complete setup
        inv = user.collector_invitations.filter(status=CollectorInvitation.Status.SENT).first()
        raw_token = "valid-setup-token-12345"
        # Manually compute matching token hash for test
        import hashlib
        inv.token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        inv.save()
        accept_collector_invitation(raw_token=raw_token, password="CollectorPassword123!")

        # Approve profile
        approve_collector_profile(actor=self.admin, user_id=user.pk)
        user.refresh_from_db()
        return user

    def _create_pending_collector(self, username="pending_col"):
        """Helper to create a pending, incomplete setup collector."""
        user = create_collector_account(
            actor=self.admin,
            username=username,
            email=f"{username}@ficol.test",
            first_name="Pending",
            last_name="Collector",
        )
        return user

    # =========================================================================
    # 1. Removal of password fields from Collector create, edit, list pages
    # =========================================================================

    def test_no_password_fields_in_collector_create_form(self):
        """CollectorCreateForm must not contain any password fields."""
        form = CollectorCreateForm()
        self.assertNotIn("password", form.fields)
        self.assertNotIn("password_confirm", form.fields)
        self.assertNotIn("new_password", form.fields)
        self.assertNotIn("new_password_confirm", form.fields)

        # GET request to collector create page
        resp = self.client.get(reverse("system_admin_collector_create"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'name="password"')
        self.assertNotContains(resp, 'name="new_password"')

    def test_no_password_fields_in_collector_edit_form(self):
        """CollectorEditForm must not contain any password or generic status fields."""
        form = CollectorEditForm()
        self.assertNotIn("new_password", form.fields)
        self.assertNotIn("new_password_confirm", form.fields)
        self.assertNotIn("password", form.fields)
        self.assertNotIn("is_active", form.fields)
        self.assertNotIn("collector_status", form.fields)

    def test_collector_edit_page_renders_no_password_inputs(self):
        """Collector edit/detail page must not render any password inputs."""
        col = self._create_active_collector("edit_view_col")
        resp = self.client.get(reverse("system_admin_collector_edit", args=[col.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'type="password"')
        self.assertNotContains(resp, 'name="new_password"')
        self.assertNotContains(resp, 'name="new_password_confirm"')
        self.assertNotContains(resp, 'name="password"')

    # =========================================================================
    # 2. Three-dot menu context-aware actions in users.html
    # =========================================================================

    def test_three_dot_menu_for_active_collector(self):
        """Active collector displays actions: View/Edit, Manage Client Access, Warnings, Suspend, Offboard, Password Reset."""
        col = self._create_active_collector("menu_active_col")
        resp = self.client.get(reverse("system_admin_users"))
        self.assertEqual(resp.status_code, 200)

        # Three-dot trigger with accessible label
        self.assertContains(resp, f'aria-label="Actions for {col.get_full_name()}"')
        self.assertContains(resp, "View / Edit Collector")
        self.assertContains(resp, "Manage Client Access")
        self.assertContains(resp, "Issue Warning")
        self.assertContains(resp, "Issue Final Warning")
        self.assertContains(resp, "Suspend Collector")
        self.assertContains(resp, "Offboard Collector")
        # Activated collector gets Send Password Reset Email, NOT Resend Setup Invitation
        self.assertContains(resp, "Send Password Reset Email")
        self.assertNotContains(resp, 'data-modal-open="reinstate"')

    def test_three_dot_menu_for_pending_incomplete_collector(self):
        """Pending collector with incomplete setup displays Resend Setup Invitation, not Password Reset."""
        col = self._create_pending_collector("menu_pending_col")
        resp = self.client.get(reverse("system_admin_users"))
        self.assertEqual(resp.status_code, 200)

        self.assertContains(resp, f'aria-label="Actions for {col.get_full_name()}"')
        self.assertContains(resp, "Resend Setup Invitation")
        self.assertNotContains(resp, "Send Password Reset Email")

    def test_three_dot_menu_for_suspended_collector(self):
        """Suspended collector displays: View / Edit, View Suspension History, Reinstate, Offboard, Password Reset."""
        col = self._create_active_collector("menu_susp_col")
        suspend_collector_profile(actor=self.admin, user_id=col.pk, reason="Temporary violation")

        resp = self.client.get(reverse("system_admin_users"))
        self.assertEqual(resp.status_code, 200)

        self.assertContains(resp, "Reinstate Collector")
        self.assertContains(resp, "View Suspension History")
        self.assertContains(resp, "Offboard Collector")
        self.assertContains(resp, "Send Password Reset Email")
        # Should not display active warning / suspend triggers
        self.assertNotContains(resp, "data-modal-open=\"suspend\"")

    def test_three_dot_menu_for_offboarded_collector(self):
        """Offboarded collector is strictly read-only: displays only View Collector History."""
        col = self._create_active_collector("menu_offboard_col")
        offboard_collector_profile(actor=self.admin, user_id=col.pk, reason="Contract concluded")

        resp = self.client.get(reverse("system_admin_users"))
        self.assertEqual(resp.status_code, 200)

        self.assertContains(resp, "View Collector History")
        self.assertNotContains(resp, "View / Edit Collector")
        self.assertNotContains(resp, 'data-modal-open="reinstate"')
        self.assertNotContains(resp, 'data-modal-open="suspend"')
        self.assertNotContains(resp, 'data-modal-open="warning"')
        self.assertNotContains(resp, "Send Password Reset Email")
        self.assertNotContains(resp, "Resend Setup Invitation")

    def test_superuser_is_protected_from_collector_actions(self):
        """Superuser account displays protected indicator without destructive or collector actions."""
        resp = self.client.get(reverse("system_admin_users"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "System Account")
        self.assertContains(resp, "Superusers are read-only system accounts")

    # =========================================================================
    # 3. Explicit Lifecycle Actions: Suspend, Reinstate, Offboard, Warnings
    # =========================================================================

    def test_suspend_requires_reason(self):
        """Suspending a collector requires a non-empty reason; empty reason raises ValidationError."""
        col = self._create_active_collector("susp_col")

        # Via service directly
        with self.assertRaises(ValidationError) as ctx:
            suspend_collector_profile(actor=self.admin, user_id=col.pk, reason="")
        self.assertIn("A reason is required to suspend a collector.", str(ctx.exception))

        with self.assertRaises(ValidationError) as ctx:
            suspend_collector_profile(actor=self.admin, user_id=col.pk, reason="   ")
        self.assertIn("A reason is required to suspend a collector.", str(ctx.exception))

        # Via HTTP POST without reason
        resp = self.client.post(
            reverse("system_admin_collector_suspend", args=[col.pk]),
            {"reason": ""},
            follow=True,
        )
        self.assertContains(resp, "A reason is required to suspend a collector.")
        col.collector_profile.refresh_from_db()
        self.assertEqual(col.collector_profile.approval_status, CollectorProfile.ApprovalStatus.APPROVED)

    def test_suspend_success_creates_disciplinary_record_and_audit(self):
        """Successful suspension updates profile status, records disciplinary record, and logs audit event."""
        col = self._create_active_collector("susp_success_col")

        resp = self.client.post(
            reverse("system_admin_collector_suspend", args=[col.pk]),
            {"reason": "Exceeded unauthorized collection radius."},
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        col.collector_profile.refresh_from_db()
        self.assertEqual(col.collector_profile.approval_status, CollectorProfile.ApprovalStatus.SUSPENDED)
        self.assertEqual(col.collector_profile.availability, CollectorProfile.Availability.OFFLINE)

        # Disciplinary record created
        record = col.collector_profile.disciplinary_records.filter(action_type=CollectorDisciplinaryRecord.ActionType.SUSPENSION).first()
        self.assertIsNotNone(record)
        self.assertEqual(record.reason, "Exceeded unauthorized collection radius.")
        self.assertEqual(record.issued_by, self.admin)

        # Audit event created
        self.assertTrue(
            AuditEvent.objects.filter(
                action="collector_suspended",
                entity_type="CollectorProfile",
                entity_id=str(col.collector_profile.pk),
            ).exists()
        )

    def test_reinstate_success_restores_approved_status_and_logs_audit(self):
        """Reinstating a suspended collector restores approved status and records note and audit log."""
        col = self._create_active_collector("reinstate_col")
        suspend_collector_profile(actor=self.admin, user_id=col.pk, reason="Initial suspension")

        resp = self.client.post(
            reverse("system_admin_collector_reinstate", args=[col.pk]),
            {"note": "Completed re-training successfully."},
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        col.collector_profile.refresh_from_db()
        self.assertEqual(col.collector_profile.approval_status, CollectorProfile.ApprovalStatus.APPROVED)

        # Disciplinary record created
        record = col.collector_profile.disciplinary_records.filter(action_type=CollectorDisciplinaryRecord.ActionType.REINSTATEMENT).first()
        self.assertIsNotNone(record)
        self.assertEqual(record.reason, "Completed re-training successfully.")

        # Audit event created
        self.assertTrue(
            AuditEvent.objects.filter(
                action="collector_reinstated",
                entity_type="CollectorProfile",
                entity_id=str(col.collector_profile.pk),
            ).exists()
        )

    def test_offboard_requires_reason_and_permanently_deactivates(self):
        """Offboarding requires a reason, sets offboarded status, deactivates User, revokes access, and logs audit."""
        col = self._create_active_collector("offboard_col")
        ClientAccess.objects.create(user=col, client=self.test_client, is_active=True)

        # Empty reason rejected
        resp = self.client.post(
            reverse("system_admin_collector_offboard", args=[col.pk]),
            {"reason": ""},
            follow=True,
        )
        self.assertContains(resp, "A reason is required to offboard a collector.")
        col.refresh_from_db()
        self.assertTrue(col.is_active)

        # Valid offboarding
        resp = self.client.post(
            reverse("system_admin_collector_offboard", args=[col.pk]),
            {"reason": "Voluntary resignation."},
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        col.refresh_from_db()
        col.collector_profile.refresh_from_db()

        self.assertFalse(col.is_active)
        self.assertEqual(col.collector_profile.approval_status, CollectorProfile.ApprovalStatus.OFFBOARDED)
        self.assertEqual(col.collector_profile.availability, CollectorProfile.Availability.OFFLINE)
        self.assertFalse(col.client_accesses.filter(is_active=True).exists())

        # Disciplinary record
        record = col.collector_profile.disciplinary_records.filter(action_type=CollectorDisciplinaryRecord.ActionType.OFFBOARDING).first()
        self.assertIsNotNone(record)
        self.assertEqual(record.reason, "Voluntary resignation.")

        # Audit event
        self.assertTrue(
            AuditEvent.objects.filter(
                action="collector_offboarded",
                entity_type="CollectorProfile",
                entity_id=str(col.collector_profile.pk),
            ).exists()
        )

    def test_offboarded_collector_is_read_only(self):
        """Edits to offboarded collectors are rejected and profile cannot be modified."""
        col = self._create_active_collector("offboarded_readonly_col")
        offboard_collector_profile(actor=self.admin, user_id=col.pk, reason="Terminated")

        # Attempt to POST updates to collector edit endpoint
        resp = self.client.post(
            reverse("system_admin_collector_edit", args=[col.pk]),
            {
                "first_name": "NewName",
                "last_name": "NewLastName",
                "email": "new@example.com",
            },
            follow=True,
        )
        self.assertContains(resp, "Offboarded collectors are read-only and cannot be modified.")
        col.refresh_from_db()
        self.assertNotEqual(col.first_name, "NewName")

        # Service level rejection
        with self.assertRaises(ValidationError) as ctx:
            update_collector_account(actor=self.admin, user_id=col.pk, first_name="Hacker")
        self.assertIn("Offboarded collectors are read-only", str(ctx.exception))

        # Attempt to reinstate offboarded collector rejected
        with self.assertRaises(ValidationError):
            reinstate_collector_profile(actor=self.admin, user_id=col.pk)

    def test_issue_warning_records_disciplinary_and_audit(self):
        """Issuing a warning or final warning requires reason, records disciplinary record, and writes audit event."""
        col = self._create_active_collector("warn_col")

        # Missing reason fails
        with self.assertRaises(ValidationError):
            issue_collector_warning(actor=self.admin, user_id=col.pk, level="warning", reason="")

        # Valid warning
        issue_collector_warning(actor=self.admin, user_id=col.pk, level="warning", reason="Late report submission")
        rec = col.collector_profile.disciplinary_records.filter(action_type=CollectorDisciplinaryRecord.ActionType.WARNING).first()
        self.assertIsNotNone(rec)
        self.assertEqual(rec.reason, "Late report submission")
        self.assertTrue(AuditEvent.objects.filter(action="collector_warning_issued").exists())

        # Valid final warning via HTTP POST
        resp = self.client.post(
            reverse("system_admin_collector_issue_warning", args=[col.pk]),
            {"level": "final_warning", "reason": "Second infraction: unverified receipt"},
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        rec2 = col.collector_profile.disciplinary_records.filter(action_type=CollectorDisciplinaryRecord.ActionType.FINAL_WARNING).first()
        self.assertIsNotNone(rec2)
        self.assertEqual(rec2.reason, "Second infraction: unverified receipt")

    # =========================================================================
    # 4. Safe Password Reset and Setup Invitation Actions
    # =========================================================================

    def test_send_password_reset_email_success_for_active_collector(self):
        """Active collector with completed setup can be sent a password reset email."""
        col = self._create_active_collector("pw_reset_col")
        mail.outbox.clear()

        resp = self.client.post(
            reverse("system_admin_collector_send_password_reset", args=[col.pk]),
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "A password reset link has been emailed to")

        # Email sent with safe link (no raw token exposed in UI)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Reset your FiCol Collector password", mail.outbox[0].subject)
        self.assertEqual(mail.outbox[0].to, [col.email])

        # Audit event recorded
        self.assertTrue(
            AuditEvent.objects.filter(
                action="collector_password_reset_sent",
                entity_type="User",
                entity_id=str(col.pk),
            ).exists()
        )

    def test_send_password_reset_rejected_for_incomplete_setup(self):
        """Sending password reset to collector with incomplete setup is rejected."""
        col = self._create_pending_collector("incomplete_pw_col")

        with self.assertRaises(ValidationError) as ctx:
            send_collector_password_reset_email(actor=self.admin, user_id=col.pk)
        self.assertIn("completed account setup", str(ctx.exception))

    def test_send_password_reset_rejected_for_offboarded_collector(self):
        """Sending password reset to an offboarded collector is rejected."""
        col = self._create_active_collector("offboarded_pw_col")
        offboard_collector_profile(actor=self.admin, user_id=col.pk, reason="Resigned")

        with self.assertRaises(ValidationError) as ctx:
            send_collector_password_reset_email(actor=self.admin, user_id=col.pk)
        self.assertIn("offboarded collector", str(ctx.exception))

    def test_resend_invitation_rejected_for_completed_setup(self):
        """Resending setup invitation is rejected for collectors who already completed setup."""
        col = self._create_active_collector("completed_resend_col")

        with self.assertRaises(ValidationError) as ctx:
            resend_collector_invitation(actor=self.admin, user_id=col.pk)
        self.assertIn("completed initial account setup", str(ctx.exception))

    # =========================================================================
    # 5. Collector Detail Page: 6 Distinct Concerns Rendered
    # =========================================================================

    def test_collector_detail_page_renders_six_concerns(self):
        """Collector detail page displays all 6 concerns separately."""
        col = self._create_active_collector("detail_col")
        # Issue a warning so disciplinary history has an entry
        issue_collector_warning(actor=self.admin, user_id=col.pk, level="warning", reason="Test record")

        resp = self.client.get(reverse("system_admin_collector_edit", args=[col.pk]))
        self.assertEqual(resp.status_code, 200)

        # 1. Profile & Basic Information
        self.assertContains(resp, "1. Profile &amp; Basic Information")
        self.assertContains(resp, col.username)
        self.assertContains(resp, col.email)

        # 2. Collector Standing
        self.assertContains(resp, "2. Collector Standing")
        self.assertContains(resp, "Active")

        # 3. Client Access Scope
        self.assertContains(resp, "3. Client Access Scope")
        self.assertContains(resp, "Alpha Corp (ALPHA)")

        # 4. Security & Invitation Status
        self.assertContains(resp, "4. Security &amp; Invitation Status")
        self.assertContains(resp, "Account Setup Status")
        self.assertContains(resp, "First Portal Visit")
        self.assertContains(resp, "Last Portal Activity")

        # 5. Warnings & Disciplinary History
        self.assertContains(resp, "5. Warnings &amp; Disciplinary History")
        self.assertContains(resp, "Test record")

        # 6. Audit Timeline
        self.assertContains(resp, "6. Audit Timeline")
        self.assertContains(resp, "collector_warning_issued")

        # Confirm no password entry fields rendered
        self.assertNotContains(resp, 'name="new_password"')
        self.assertNotContains(resp, 'name="new_password_confirm"')

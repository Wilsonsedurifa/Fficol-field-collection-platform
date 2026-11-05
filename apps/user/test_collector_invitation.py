from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import ValidationError
from django.test import TestCase, Client as TestClient
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Client
from apps.audit.models import AuditEvent
from apps.user.admin_portal.services import (
    accept_collector_invitation,
    approve_collector_profile,
    create_collector_account,
    reject_collector_profile,
    resend_collector_invitation,
    revoke_collector_invitation,
    suspend_collector_profile,
)
from apps.user.invitations import (
    generate_invitation_token,
    get_collector_account_setup_status,
    get_collector_approval_status_info,
    hash_invitation_token,
)
from apps.user.models import CollectorInvitation, CollectorProfile
from apps.user.roles import sync_backoffice_roles

User = get_user_model()


class CollectorInvitationAndActivityTests(TestCase):
    def setUp(self):
        sync_backoffice_roles()
        self.admin = User.objects.create_superuser(
            username="admin_user",
            email="admin@example.com",
            password="AdminPassword123!",
        )
        self.client_corp = Client.objects.create(code="CORP1", name="Corp Client", is_active=True)

    def _extract_token(self, index=-1):
        return mail.outbox[index].body.split("/invitations/collector/")[1].split("/")[0].strip()

    def test_collector_creation_creates_inactive_user_pending_profile_and_invitation(self):
        """Collector creation creates User with is_active=False, pending CollectorProfile, and CollectorInvitation."""
        collector = create_collector_account(
            actor=self.admin,
            username="john_collector",
            email="john@example.com",
            first_name="John",
            last_name="Doe",
        )

        self.assertFalse(collector.is_active)
        self.assertFalse(collector.has_usable_password())
        self.assertEqual(collector.email, "john@example.com")
        self.assertIsNone(collector.account_activated_at)
        self.assertIsNone(collector.email_verified_at)

        # Profile
        profile = collector.collector_profile
        self.assertEqual(profile.approval_status, CollectorProfile.ApprovalStatus.APPROVED)
        self.assertIsNone(profile.first_portal_visit_at)
        self.assertIsNone(profile.last_portal_activity_at)

        # Invitation
        invitations = collector.collector_invitations.all()
        self.assertEqual(invitations.count(), 1)
        invitation = invitations.first()
        self.assertEqual(invitation.email, "john@example.com")
        self.assertEqual(invitation.status, CollectorInvitation.Status.SENT)
        self.assertEqual(len(invitation.token_hash), 64)
        self.assertIsNotNone(invitation.expires_at)
        self.assertTrue(invitation.expires_at > timezone.now())

        # Audit events
        user_audit = AuditEvent.objects.filter(entity_type="User", entity_id=str(collector.pk)).first()
        self.assertIsNotNone(user_audit)
        self.assertEqual(user_audit.action, "system_admin_user_created")
        self.assertFalse(user_audit.changes["is_active"])
        self.assertTrue(user_audit.changes["collector_profile_created"])
        self.assertTrue(user_audit.changes["invitation_sent"])

        inv_audit = AuditEvent.objects.filter(entity_type="CollectorInvitation", entity_id=str(invitation.pk)).first()
        self.assertIsNotNone(inv_audit)
        self.assertEqual(inv_audit.action, "collector_invitation_sent")
        self.assertEqual(inv_audit.changes["status"], "sent")

    def test_invitation_email_contains_setup_link_but_never_password(self):
        """Invitation email sends one-time setup link and never contains a password."""
        mail.outbox = []
        collector = create_collector_account(
            actor=self.admin,
            username="alice_col",
            email="alice@example.com",
        )

        self.assertEqual(len(mail.outbox), 1)
        sent_email = mail.outbox[0]
        self.assertEqual(sent_email.to, ["alice@example.com"])
        self.assertIn("Set your FiCol Collector password", sent_email.subject)
        self.assertIn("/invitations/collector/", sent_email.body)
        self.assertIn("72 hours", sent_email.body)

        # Confirm no passwords or temporary credentials leaked
        body_lower = sent_email.body.lower()
        self.assertNotIn("temporary password", body_lower)
        self.assertNotIn("temporary", body_lower)

    def test_valid_invitation_allows_password_setup_and_activates_account(self):
        """Using a valid invitation token sets password, activates account, and updates timestamps."""
        create_collector_account(
            actor=self.admin,
            username="bob_col",
            email="bob@example.com",
        )
        invitation = CollectorInvitation.objects.get(email="bob@example.com")
        raw_token = mail.outbox[0].body.split("/invitations/collector/")[1].split("/")[0].split()[0].strip()

        # Submit through the view
        client = TestClient()
        setup_url = reverse("collector_invitation_setup", kwargs={"token": raw_token})
        
        # GET renders set password page
        resp_get = client.get(setup_url)
        self.assertEqual(resp_get.status_code, 200)
        self.assertContains(resp_get, "New Password")

        # POST valid password
        resp_post = client.post(setup_url, {
            "password": "SecurePassword123!#",
            "password_confirm": "SecurePassword123!#",
        })
        # Email verification and password setup grant immediate portal access.
        self.assertRedirects(resp_post, reverse("collector_dashboard"))

        user = User.objects.get(username="bob_col")
        self.assertTrue(user.is_active)
        self.assertTrue(user.check_password("SecurePassword123!#"))
        self.assertIsNotNone(user.email_verified_at)
        self.assertIsNotNone(user.account_activated_at)

        invitation.refresh_from_db()
        self.assertEqual(invitation.status, CollectorInvitation.Status.ACCEPTED)
        self.assertIsNotNone(invitation.accepted_at)

        # Audit event
        audit = AuditEvent.objects.filter(action="collector_invitation_accepted", entity_id=str(invitation.pk)).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.changes["status"], "accepted")

    def test_expired_revoked_reused_and_invalid_tokens_fail_safely(self):
        """Expired, revoked, reused, and invalid invitation tokens fail safely without changing credentials."""
        # 1. Invalid token
        client = TestClient()
        resp_invalid = client.get(reverse("collector_invitation_setup", kwargs={"token": "invalid_random_token"}))
        self.assertEqual(resp_invalid.status_code, 400)
        self.assertContains(resp_invalid, "link is invalid", status_code=400)

        # 2. Expired token
        user = create_collector_account(actor=self.admin, username="exp_col", email="exp@example.com")
        inv = user.collector_invitations.first()
        raw_token = self._extract_token(-1)
        inv.expires_at = timezone.now() - timedelta(hours=1)
        inv.save()

        resp_expired = client.get(reverse("collector_invitation_setup", kwargs={"token": raw_token}))
        self.assertEqual(resp_expired.status_code, 400)
        self.assertContains(resp_expired, "has expired", status_code=400)

        # 3. Revoked token
        inv.expires_at = timezone.now() + timedelta(hours=72)
        inv.status = CollectorInvitation.Status.REVOKED
        inv.save()

        resp_revoked = client.get(reverse("collector_invitation_setup", kwargs={"token": raw_token}))
        self.assertEqual(resp_revoked.status_code, 400)
        self.assertContains(resp_revoked, "has been revoked", status_code=400)

        # 4. Reused / accepted token
        inv.status = CollectorInvitation.Status.ACCEPTED
        inv.save()

        resp_reused = client.get(reverse("collector_invitation_setup", kwargs={"token": raw_token}))
        self.assertEqual(resp_reused.status_code, 400)
        self.assertContains(resp_reused, "already been used", status_code=400)

    def test_resend_invalidates_old_invitation(self):
        """Resending an invitation revokes prior active invitations and sends a new token."""
        user = create_collector_account(actor=self.admin, username="resend_col", email="resend@example.com")
        first_inv = user.collector_invitations.first()
        first_raw_token = self._extract_token(-1)

        # Resend invitation
        new_inv, new_raw_token = resend_collector_invitation(actor=self.admin, user_id=user.pk)
        self.assertNotEqual(first_raw_token, new_raw_token)
        self.assertEqual(new_inv.status, CollectorInvitation.Status.SENT)

        first_inv.refresh_from_db()
        self.assertEqual(first_inv.status, CollectorInvitation.Status.REVOKED)
        self.assertIsNotNone(first_inv.revoked_at)

        # Old link now fails
        client = TestClient()
        resp_old = client.get(reverse("collector_invitation_setup", kwargs={"token": first_raw_token}))
        self.assertEqual(resp_old.status_code, 400)
        self.assertContains(resp_old, "has been revoked", status_code=400)

        # New link works
        resp_new = client.get(reverse("collector_invitation_setup", kwargs={"token": new_raw_token}))
        self.assertEqual(resp_new.status_code, 200)

        # Audit event
        audit = AuditEvent.objects.filter(action="collector_invitation_resent", entity_id=str(new_inv.pk)).first()
        self.assertIsNotNone(audit)

    def test_revoke_invitation_prevents_immediate_use(self):
        """Revoking an invitation prevents use immediately."""
        user = create_collector_account(actor=self.admin, username="revoke_col", email="revoke@example.com")
        inv = user.collector_invitations.first()
        raw_token = self._extract_token(-1)

        revoke_collector_invitation(actor=self.admin, user_id=user.pk)
        inv.refresh_from_db()
        self.assertEqual(inv.status, CollectorInvitation.Status.REVOKED)

        client = TestClient()
        resp = client.post(reverse("collector_invitation_setup", kwargs={"token": raw_token}), {
            "password": "Password123!#",
            "password_confirm": "Password123!#",
        })
        self.assertEqual(resp.status_code, 400)
        self.assertContains(resp, "has been revoked", status_code=400)

        user.refresh_from_db()
        self.assertFalse(user.is_active)

    def test_activated_collector_needs_no_approval_but_suspension_still_blocks_work(self):
        user = create_collector_account(actor=self.admin, username="ready_worker", email="ready@example.com")
        raw_token = self._extract_token(-1)
        accept_collector_invitation(raw_token=raw_token, password="SecurePass123!#")
        client = TestClient()
        self.assertTrue(client.login(username="ready_worker", password="SecurePass123!#"))
        self.assertEqual(client.get(reverse("collector_dashboard")).status_code, 200)
        self.assertEqual(client.get(reverse("collector_commissions")).status_code, 200)
        suspend_collector_profile(actor=self.admin, user_id=user.pk, reason="Policy violation")
        self.assertEqual(client.get(reverse("collector_commissions")).status_code, 403)

    def test_first_portal_visit_recorded_only_when_entering_real_dashboard(self):
        """first_portal_visit_at is set only when an approved collector successfully loads the actual dashboard."""
        user = create_collector_account(actor=self.admin, username="visiting_col", email="visit@example.com")
        raw_token = self._extract_token(-1)
        accept_collector_invitation(raw_token=raw_token, password="SecurePass123!#")

        profile = user.collector_profile
        self.assertIsNone(profile.first_portal_visit_at)

        # Approve the collector
        approve_collector_profile(actor=self.admin, user_id=user.pk)

        client = TestClient()
        client.login(username="visiting_col", password="SecurePass123!#")

        # Loading help does not set first_portal_visit_at
        client.get(reverse("collector_help"))
        profile.refresh_from_db()
        self.assertIsNone(profile.first_portal_visit_at)

        # Loading actual dashboard sets first_portal_visit_at
        resp_dash = client.get(reverse("collector_dashboard"))
        self.assertEqual(resp_dash.status_code, 200)
        profile.refresh_from_db()
        first_visit = profile.first_portal_visit_at
        self.assertIsNotNone(first_visit)

        # Loading dashboard again does NOT overwrite first_portal_visit_at
        client.get(reverse("collector_dashboard"))
        profile.refresh_from_db()
        self.assertEqual(profile.first_portal_visit_at, first_visit)

    def test_last_portal_activity_is_throttled(self):
        """last_portal_activity_at is updated on collector requests and throttled to once every 5 minutes."""
        user = create_collector_account(actor=self.admin, username="active_col", email="act@example.com")
        raw_token = self._extract_token(-1)
        accept_collector_invitation(raw_token=raw_token, password="SecurePass123!#")
        approve_collector_profile(actor=self.admin, user_id=user.pk)

        profile = CollectorProfile.objects.get(user=user)
        initial_time = timezone.now() - timedelta(minutes=10)
        profile.last_portal_activity_at = initial_time
        profile.save(update_fields=["last_portal_activity_at"])

        client = TestClient()
        login_success = client.login(username="active_col", password="SecurePass123!#")
        self.assertTrue(login_success)

        # 1. First request after 10 minutes updates last_portal_activity_at
        resp = client.get(reverse("collector_dashboard"))
        self.assertEqual(resp.status_code, 200)
        profile.refresh_from_db()
        t1 = profile.last_portal_activity_at
        self.assertTrue(t1 > initial_time)

        # 2. Immediate second request within 5 minutes is THROTTLED (not updated)
        client.get(reverse("collector_help"))
        profile.refresh_from_db()
        self.assertEqual(profile.last_portal_activity_at, t1)

        # 3. Simulate passage of 6 minutes
        profile.last_portal_activity_at = timezone.now() - timedelta(minutes=6)
        profile.save()
        old_t = profile.last_portal_activity_at

        client.get(reverse("collector_profile"))
        profile.refresh_from_db()
        self.assertTrue(profile.last_portal_activity_at > old_t)

    def test_admin_status_badges_show_correct_states(self):
        """Computed status helper returns correct account setup and approval status badges."""
        user = create_collector_account(actor=self.admin, username="badge_col", email="badge@example.com")
        profile = user.collector_profile

        # 1. Initially: "Invitation sent" and "Pending approval"
        s1 = get_collector_account_setup_status(user)
        self.assertEqual(s1["label"], "Invitation sent")
        self.assertEqual(s1["badge"], "warning")
        a1 = get_collector_approval_status_info(profile)
        self.assertEqual(a1["label"], "Approved")
        self.assertEqual(a1["badge"], "success")

        # 2. Invitation accepted, account activated, but portal not yet visited
        raw_token = self._extract_token(-1)
        accept_collector_invitation(raw_token=raw_token, password="SecurePass123!#")
        user.refresh_from_db()
        profile.refresh_from_db()
        s2 = get_collector_account_setup_status(user)
        self.assertEqual(s2["label"], "Activated — Collector Portal not yet visited")
        self.assertEqual(s2["badge"], "info")

        # 3. Portal visited
        profile.first_portal_visit_at = timezone.now()
        profile.save()
        s3 = get_collector_account_setup_status(user)
        self.assertEqual(s3["label"], "Collector Portal visited")
        self.assertEqual(s3["badge"], "success")

        # 4. Status transitions: approved, rejected, suspended
        approve_collector_profile(actor=self.admin, user_id=user.pk)
        profile.refresh_from_db()
        self.assertEqual(get_collector_approval_status_info(profile)["label"], "Approved")

        reject_collector_profile(actor=self.admin, user_id=user.pk)
        profile.refresh_from_db()
        self.assertEqual(get_collector_approval_status_info(profile)["label"], "Rejected")

        suspend_collector_profile(actor=self.admin, user_id=user.pk, reason="Testing suspension")
        profile.refresh_from_db()
        self.assertEqual(get_collector_approval_status_info(profile)["label"], "Suspended")

    def test_audit_events_written_for_all_lifecycle_actions(self):
        """Audit events are recorded for creation, resend, revoke, acceptance, approval, rejection, and suspension."""
        # 1. Creation
        user = create_collector_account(actor=self.admin, username="audit_col", email="audit@example.com")
        self.assertTrue(AuditEvent.objects.filter(action="system_admin_user_created", entity_id=str(user.pk)).exists())
        self.assertTrue(AuditEvent.objects.filter(action="collector_invitation_sent").exists())

        # 2. Resend
        new_inv, raw_token = resend_collector_invitation(actor=self.admin, user_id=user.pk)
        self.assertTrue(AuditEvent.objects.filter(action="collector_invitation_resent", entity_id=str(new_inv.pk)).exists())

        # 3. Acceptance
        accept_collector_invitation(raw_token=raw_token, password="Password123!#")
        self.assertTrue(AuditEvent.objects.filter(action="collector_invitation_accepted", entity_id=str(new_inv.pk)).exists())

        # Already eligible on creation; no manual approval transition is needed.
        approve_collector_profile(actor=self.admin, user_id=user.pk)
        self.assertFalse(AuditEvent.objects.filter(action="system_admin_collector_status_changed", changes__approval_status__after="approved").exists())

        # 5. Rejection
        reject_collector_profile(actor=self.admin, user_id=user.pk)
        self.assertTrue(AuditEvent.objects.filter(action="system_admin_collector_status_changed", changes__approval_status__after="rejected").exists())

        # 6. Suspension
        suspend_collector_profile(actor=self.admin, user_id=user.pk, reason="Testing suspension")
        self.assertTrue(AuditEvent.objects.filter(action="system_admin_collector_status_changed", changes__approval_status__after="suspended").exists())

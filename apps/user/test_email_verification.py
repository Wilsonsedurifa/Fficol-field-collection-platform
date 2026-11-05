from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import ValidationError
from django.test import Client as TestClient, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Client
from apps.audit.models import AuditEvent
from apps.user.admin_portal.forms import CollectorCreateForm, CollectorEditForm
from apps.user.admin_portal.services import (
    accept_collector_invitation,
    approve_collector_profile,
    create_collector_account,
    resend_collector_invitation,
    revoke_collector_invitation,
    update_collector_account,
)
from apps.user.invitations import (
    generate_invitation_token,
    get_collector_badge_statuses,
    hash_invitation_token,
)
from apps.user.models import CollectorInvitation, CollectorProfile
from apps.user.roles import sync_backoffice_roles

User = get_user_model()


class EmailVerificationAndBadgeLifecycleTests(TestCase):
    def setUp(self):
        sync_backoffice_roles()
        self.admin = User.objects.create_superuser(
            username="admin_user",
            email="admin@ficol.local",
            password="AdminPassword123!",
        )
        self.client_corp = Client.objects.create(code="CORP1", name="Corp Client", is_active=True)
        self.client = TestClient()
        self.client.force_login(self.admin)

    def _extract_token(self, index=-1):
        """Extract the raw invitation token from the email in outbox."""
        body = mail.outbox[index].body
        return body.split("/invitations/collector/")[1].split("/")[0].strip()

    # =========================================================================
    # 1. Email Format Validation & Lowercase Normalization
    # =========================================================================

    def test_email_format_validation_on_create(self):
        """Invalid email formats are rejected during Collector creation in forms and services."""
        # Form validation
        form = CollectorCreateForm(data={"username": "col1", "email": "invalid-email-address"})
        self.assertFalse(form.is_valid())
        self.assertIn("email", form.errors)

        # Service validation
        with self.assertRaises(ValidationError):
            create_collector_account(
                actor=self.admin,
                username="col1",
                email="not-an-email",
            )

    def test_email_normalization_on_create_and_edit(self):
        """Email is trimmed and lowercased on creation and editing."""
        raw_email = "   COLLECTOR.Test@Domain.COM   "
        expected_email = "collector.test@domain.com"

        # Via form clean
        form = CollectorCreateForm(data={"username": "col_norm", "email": raw_email})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["email"], expected_email)

        # Via service create
        collector = create_collector_account(
            actor=self.admin,
            username="col_norm",
            email=raw_email,
        )
        self.assertEqual(collector.email, expected_email)

        # Via edit form clean
        edit_form = CollectorEditForm(
            data={"email": "   Updated.Email@OtherDomain.COM   "},
            target_user=collector,
        )
        self.assertTrue(edit_form.is_valid(), edit_form.errors)
        self.assertEqual(edit_form.cleaned_data["email"], "updated.email@otherdomain.com")

        # Via service update
        update_collector_account(
            actor=self.admin,
            user_id=collector.pk,
            email="   NEW.Normalized@Domain.COM   ",
        )
        collector.refresh_from_db()
        self.assertEqual(collector.email, "new.normalized@domain.com")

    # =========================================================================
    # 2. Duplicate Active Account Prevention
    # =========================================================================

    def test_prevent_duplicate_active_accounts_on_create(self):
        """Prevent creating an account with an email used by an existing active user."""
        User.objects.create_user(
            username="existing_active",
            email="existing@example.com",
            is_active=True,
        )

        # Form rejects duplicate active email case-insensitively
        form = CollectorCreateForm(data={"username": "new_col", "email": "EXISTING@EXAMPLE.COM"})
        self.assertFalse(form.is_valid())
        self.assertIn("An active account with this email address already exists.", str(form.errors))

        # Service rejects duplicate active email
        with self.assertRaises(ValidationError) as ctx:
            create_collector_account(
                actor=self.admin,
                username="new_col",
                email="EXISTING@example.com",
            )
        self.assertIn("active account", str(ctx.exception).lower())

    def test_allow_email_if_existing_account_is_inactive(self):
        """An inactive account with the same email does not block creation if allowed by rule."""
        User.objects.create_user(
            username="inactive_user",
            email="inactive@example.com",
            is_active=False,
        )

        form = CollectorCreateForm(data={"username": "new_col_inactive", "email": "inactive@example.com"})
        self.assertTrue(form.is_valid(), form.errors)

    def test_prevent_duplicate_active_accounts_on_edit(self):
        """Prevent updating a collector's email to match another active user's email."""
        User.objects.create_user(
            username="other_active",
            email="other@example.com",
            is_active=True,
        )
        collector = create_collector_account(
            actor=self.admin,
            username="my_col",
            email="mycol@example.com",
        )

        form = CollectorEditForm(
            data={"email": "OTHER@EXAMPLE.COM"},
            target_user=collector,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("An active account with this email address already exists.", str(form.errors))

        with self.assertRaises(ValidationError):
            update_collector_account(
                actor=self.admin,
                user_id=collector.pk,
                email="other@example.com",
            )

    # =========================================================================
    # 3. Initial Account Creation State & Badges
    # =========================================================================

    def test_initial_collector_creation_state_and_badges(self):
        """When created: email_verified_at=None, account awaiting activation, invitation sent."""
        collector = create_collector_account(
            actor=self.admin,
            username="init_col",
            email="init@example.com",
            first_name="Init",
            last_name="Collector",
        )

        self.assertFalse(collector.is_active)
        self.assertIsNone(collector.email_verified_at)
        self.assertIsNone(collector.account_activated_at)

        # Check badges
        badges = get_collector_badge_statuses(collector)
        self.assertEqual(badges["email"]["status"], "verification_sent")
        self.assertEqual(badges["email"]["label"], "Verification sent")
        self.assertEqual(badges["email"]["badge"], "info")

        self.assertEqual(badges["invitation"]["status"], "sent")
        self.assertEqual(badges["invitation"]["label"], "Sent")
        self.assertEqual(badges["invitation"]["badge"], "info")

        self.assertEqual(badges["account"]["status"], "awaiting_activation")
        self.assertEqual(badges["account"]["label"], "Awaiting activation")
        self.assertEqual(badges["account"]["badge"], "warning")

        self.assertEqual(badges["standing"]["status"], "pending")
        self.assertEqual(badges["standing"]["label"], "Pending")

    def test_email_sent_is_not_proof_of_ownership(self):
        """Email dispatch creates an invitation but does NOT verify email or activate account."""
        collector = create_collector_account(
            actor=self.admin,
            username="unverified_col",
            email="unverified@example.com",
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertIsNone(collector.email_verified_at)
        self.assertIsNone(collector.account_activated_at)
        self.assertFalse(collector.is_active)

    # =========================================================================
    # 4. Setup Completion: Verification, Activation & Accepted Invitation
    # =========================================================================

    def test_invitation_acceptance_verifies_email_and_activates_account(self):
        """Opening a valid link and setting a password verifies email, activates account, and marks invitation accepted."""
        collector = create_collector_account(
            actor=self.admin,
            username="accept_col",
            email="accept@example.com",
        )
        raw_token = self._extract_token()

        # Approve profile first so standing becomes active upon account activation
        approve_collector_profile(actor=self.admin, user_id=collector.pk)

        # Accept invitation via service
        accept_collector_invitation(raw_token=raw_token, password="CollectorPass123!#")

        collector.refresh_from_db()
        self.assertTrue(collector.is_active)
        self.assertIsNotNone(collector.email_verified_at)
        self.assertIsNotNone(collector.account_activated_at)

        latest_inv = collector.collector_invitations.first()
        self.assertEqual(latest_inv.status, CollectorInvitation.Status.ACCEPTED)
        self.assertIsNotNone(latest_inv.accepted_at)

        # Audit event logged
        self.assertTrue(
            AuditEvent.objects.filter(
                action="collector_invitation_accepted",
                entity_type="CollectorInvitation",
                entity_id=latest_inv.pk,
            ).exists()
        )

        # Badges after successful setup
        badges = get_collector_badge_statuses(collector)
        self.assertEqual(badges["email"]["status"], "verified")
        self.assertEqual(badges["email"]["label"], "Verified")
        self.assertEqual(badges["email"]["badge"], "success")

        self.assertEqual(badges["invitation"]["status"], "accepted")
        self.assertEqual(badges["invitation"]["label"], "Accepted")
        self.assertEqual(badges["invitation"]["badge"], "success")

        self.assertEqual(badges["account"]["status"], "active")
        self.assertEqual(badges["account"]["label"], "Active")
        self.assertEqual(badges["account"]["badge"], "success")

        self.assertEqual(badges["standing"]["status"], "active")
        self.assertEqual(badges["standing"]["label"], "Active")
        self.assertEqual(badges["standing"]["badge"], "success")

    # =========================================================================
    # 5. Invalid / Expired / Revoked Failure Modes
    # =========================================================================

    def test_expired_invitation_fails_safely_without_verifying(self):
        """Expired link cannot be used; email remains unverified and account inactive."""
        collector = create_collector_account(
            actor=self.admin,
            username="expired_col",
            email="expired@example.com",
        )
        raw_token = self._extract_token()
        inv = collector.collector_invitations.first()
        inv.expires_at = timezone.now() - timedelta(minutes=1)
        inv.save(update_fields=["expires_at"])

        with self.assertRaises(ValidationError):
            accept_collector_invitation(raw_token=raw_token, password="NewPassword123!")

        collector.refresh_from_db()
        self.assertIsNone(collector.email_verified_at)
        self.assertFalse(collector.is_active)

        badges = get_collector_badge_statuses(collector)
        self.assertEqual(badges["email"]["label"], "Unverified")
        self.assertEqual(badges["invitation"]["label"], "Expired")
        self.assertEqual(badges["account"]["label"], "Awaiting activation")

    def test_revoked_invitation_fails_safely(self):
        """Revoked link cannot be used; email remains unverified."""
        collector = create_collector_account(
            actor=self.admin,
            username="revoked_col",
            email="revoked@example.com",
        )
        raw_token = self._extract_token()
        revoke_collector_invitation(actor=self.admin, user_id=collector.pk)

        with self.assertRaises(ValidationError):
            accept_collector_invitation(raw_token=raw_token, password="NewPassword123!")

        collector.refresh_from_db()
        self.assertIsNone(collector.email_verified_at)
        self.assertFalse(collector.is_active)

        badges = get_collector_badge_statuses(collector)
        self.assertEqual(badges["email"]["label"], "Unverified")
        self.assertEqual(badges["invitation"]["label"], "Revoked")
        self.assertEqual(badges["account"]["label"], "Awaiting activation")

    def test_single_use_invitation_cannot_be_reused(self):
        """Accepted invitation cannot be used a second time."""
        collector = create_collector_account(
            actor=self.admin,
            username="reuse_col",
            email="reuse@example.com",
        )
        raw_token = self._extract_token()
        accept_collector_invitation(raw_token=raw_token, password="PasswordOne123!")

        with self.assertRaises(ValidationError):
            accept_collector_invitation(raw_token=raw_token, password="PasswordTwo123!")

    # =========================================================================
    # 6. Resending Verification
    # =========================================================================

    def test_resending_verification_revokes_prior_and_issues_fresh_token(self):
        """Resending verification revokes earlier active token, sets fresh 72h expiry, and leaves email unverified."""
        collector = create_collector_account(
            actor=self.admin,
            username="resend_col",
            email="resend@example.com",
        )
        first_token = self._extract_token(0)
        first_inv = collector.collector_invitations.first()

        resend_collector_invitation(actor=self.admin, user_id=collector.pk)

        first_inv.refresh_from_db()
        self.assertEqual(first_inv.status, CollectorInvitation.Status.REVOKED)
        self.assertIsNotNone(first_inv.revoked_at)

        # First token can no longer be used
        with self.assertRaises(ValidationError):
            accept_collector_invitation(raw_token=first_token, password="NewPass123!")

        # Second token is valid
        second_token = self._extract_token(1)
        second_inv = collector.collector_invitations.latest("created_at")
        self.assertEqual(second_inv.status, CollectorInvitation.Status.SENT)
        self.assertGreater(second_inv.expires_at, timezone.now() + timedelta(hours=71))

        # Email is still unverified until link accepted
        collector.refresh_from_db()
        self.assertIsNone(collector.email_verified_at)

        accept_collector_invitation(raw_token=second_token, password="ValidPass123!")
        collector.refresh_from_db()
        self.assertIsNotNone(collector.email_verified_at)
        self.assertTrue(collector.is_active)

    # =========================================================================
    # 7. Changing Collector Email Lifecycle
    # =========================================================================

    def test_changing_collector_email_resets_verification_and_dispatches_invite(self):
        """Changing email:
        - immediately clears email_verified_at
        - resets account setup status back to Awaiting Activation (is_active=False, unusable password)
        - revokes active invitations
        - sends a new verification invitation
        - logs collector_email_changed and collector_invitation_sent audit events.
        """
        collector = create_collector_account(
            actor=self.admin,
            username="change_col",
            email="old.email@example.com",
        )
        raw_token = self._extract_token()
        approve_collector_profile(actor=self.admin, user_id=collector.pk)
        accept_collector_invitation(raw_token=raw_token, password="InitialPass123!")

        collector.refresh_from_db()
        self.assertTrue(collector.is_active)
        self.assertIsNotNone(collector.email_verified_at)
        self.assertIsNotNone(collector.account_activated_at)

        old_inv = collector.collector_invitations.first()
        self.assertEqual(old_inv.status, CollectorInvitation.Status.ACCEPTED)

        # Clear outbox
        mail.outbox.clear()

        # Update email to new address
        update_collector_account(
            actor=self.admin,
            user_id=collector.pk,
            email="new.email@example.com",
        )

        collector.refresh_from_db()
        self.assertEqual(collector.email, "new.email@example.com")
        self.assertIsNone(collector.email_verified_at)
        self.assertIsNone(collector.account_activated_at)
        self.assertFalse(collector.is_active)
        self.assertFalse(collector.has_usable_password())

        # Email dispatched
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("new.email@example.com", mail.outbox[0].to)

        # Audit events written
        self.assertTrue(
            AuditEvent.objects.filter(
                action="collector_email_changed",
                entity_type="User",
                entity_id=collector.pk,
            ).exists()
        )
        self.assertTrue(
            AuditEvent.objects.filter(
                action="collector_invitation_sent",
                entity_type="CollectorInvitation",
            ).exists()
        )

        # Check badges immediately after email change
        badges = get_collector_badge_statuses(collector)
        self.assertEqual(badges["email"]["label"], "Verification sent")
        self.assertEqual(badges["invitation"]["label"], "Sent")
        self.assertEqual(badges["account"]["label"], "Awaiting activation")
        self.assertEqual(badges["standing"]["label"], "Active")

        # Now collector completes setup via new link
        new_token = self._extract_token(0)
        accept_collector_invitation(raw_token=new_token, password="BrandNewPassword123!")

        collector.refresh_from_db()
        self.assertTrue(collector.is_active)
        self.assertIsNotNone(collector.email_verified_at)
        self.assertIsNotNone(collector.account_activated_at)

        badges_after = get_collector_badge_statuses(collector)
        self.assertEqual(badges_after["email"]["label"], "Verified")
        self.assertEqual(badges_after["invitation"]["label"], "Accepted")
        self.assertEqual(badges_after["account"]["label"], "Active")
        self.assertEqual(badges_after["standing"]["label"], "Active")

    # =========================================================================
    # 8. Example UI Row Matching Prompt Specification
    # =========================================================================

    def test_example_ui_row_scenario(self):
        """Matches the exact example from the prompt:
        Example UI row:
          Email: Unverified
          Invitation: Revoked
          Account: Awaiting activation
          Collector: Active

        After successful setup:
          Email: Verified
          Invitation: Accepted
          Account: Active
          Collector: Active
        """
        # Create collector with approved profile
        collector = create_collector_account(
            actor=self.admin,
            username="example_row_col",
            email="example.row@example.com",
        )
        approve_collector_profile(actor=self.admin, user_id=collector.pk)

        # Revoke the invitation
        revoke_collector_invitation(actor=self.admin, user_id=collector.pk)
        collector.refresh_from_db()

        # Check state:
        # Email: Unverified
        # Invitation: Revoked
        # Account: Awaiting activation
        # Collector: Active
        badges = get_collector_badge_statuses(collector)
        self.assertEqual(badges["email"]["label"], "Unverified")
        self.assertEqual(badges["invitation"]["label"], "Revoked")
        self.assertEqual(badges["account"]["label"], "Awaiting activation")
        self.assertEqual(badges["standing"]["label"], "Active")

        # Now resend invitation and accept it
        resend_collector_invitation(actor=self.admin, user_id=collector.pk)
        raw_token = self._extract_token()
        accept_collector_invitation(raw_token=raw_token, password="CompletedPassword123!")

        collector.refresh_from_db()
        badges_after = get_collector_badge_statuses(collector)
        self.assertEqual(badges_after["email"]["label"], "Verified")
        self.assertEqual(badges_after["invitation"]["label"], "Accepted")
        self.assertEqual(badges_after["account"]["label"], "Active")
        self.assertEqual(badges_after["standing"]["label"], "Active")

    # =========================================================================
    # 9. UI Rendering: Users List & Collector Detail
    # =========================================================================

    def test_users_list_renders_status_badge_for_collector(self):
        """The users list table renders status and role for collectors."""
        collector = create_collector_account(
            actor=self.admin,
            username="badge_ui_col",
            email="badge_ui@example.com",
        )
        response = self.client.get(reverse("system_admin_users"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "badge_ui_col")
        self.assertContains(response, "badge_ui@example.com")
        self.assertContains(response, "Collector")
        self.assertContains(response, "Awaiting setup")

    def test_collector_edit_renders_account_details(self):
        """The collector edit page renders account details with email and invitation status."""
        collector = create_collector_account(
            actor=self.admin,
            username="edit_ui_col",
            email="edit_ui@example.com",
        )
        response = self.client.get(reverse("system_admin_collector_edit", args=[collector.pk]))
        self.assertEqual(response.status_code, 200)

        self.assertContains(response, "Account details")
        self.assertContains(response, "Email: Unverified")
        self.assertContains(response, "Invitation: Sent")

    # =========================================================================
    # 10. Collector Account Activation Page Copy & Accessible Semantics
    # =========================================================================

    def test_collector_activation_page_copy_and_accessibility(self):
        """Verify the exact copy, placeholders, requirements, labels, and footer note on the activation page."""
        collector = create_collector_account(
            actor=self.admin,
            username="copy_test_col",
            email="copy_test@example.com",
            first_name="Alex",
            last_name="Reyes",
        )
        raw_token = self._extract_token()
        url = reverse("collector_invitation_setup", kwargs={"token": raw_token})

        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        # 1. Intro text
        self.assertContains(response, "Welcome, <strong>Alex Reyes</strong>!")
        self.assertContains(response, "Create a password to activate your Collector account.")
        self.assertNotContains(response, "Please set a strong password")

        # 2. Placeholders and labels
        self.assertContains(response, "New Password")
        self.assertContains(response, "Confirm New Password")
        self.assertContains(response, 'placeholder="Enter your new password"')
        self.assertContains(response, 'placeholder="Re-enter your new password"')

        # 3. Requirements list matching AUTH_PASSWORD_VALIDATORS and form validation
        self.assertContains(response, "At least 8 characters")
        self.assertNotContains(response, "Cannot be entirely numeric")
        self.assertNotContains(response, "Cannot be a commonly used password")
        self.assertNotContains(response, "Cannot be too similar to your personal information")
        self.assertContains(response, "Passwords match")
        self.assertNotContains(response, "Passwords must match")

        # 4. Button label
        self.assertContains(response, "Set Password &amp; Activate Account")
        self.assertNotContains(response, "Activate Account &amp; Set Password")

        # 5. Footer note with shared expiry hours and dynamic current year
        self.assertContains(response, "One-time secure link &bull; Expires in 72 hours")
        current_year = str(timezone.now().year)
        self.assertContains(response, f"&copy; {current_year} FiCol Systems. All rights reserved.")

        # 7. Show/Hide toggle aria-labels and initial text
        self.assertContains(response, 'aria-label="Show password"')
        self.assertContains(response, '<span id="toggle-pwd1-text">Show</span>')
        self.assertContains(response, '<span id="toggle-pwd2-text">Show</span>')

    def test_collector_activation_error_messages_copy(self):
        """Verify plain English copy and clear next steps for invalid, expired, revoked, and used links."""
        # Invalid link
        response = self.client.get(reverse("collector_invitation_setup", kwargs={"token": "invalid-token-123"}))
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "This invitation link is invalid. Please ask your administrator to send a new invitation.", status_code=400)

        # Revoked link
        collector = create_collector_account(
            actor=self.admin,
            username="revoked_copy_col",
            email="revoked_copy@example.com",
        )
        raw_token = self._extract_token()
        revoke_collector_invitation(actor=self.admin, user_id=collector.pk)
        response = self.client.get(reverse("collector_invitation_setup", kwargs={"token": raw_token}))
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "This invitation has been revoked. Please ask your administrator to send a new invitation.", status_code=400)

        # Expired link
        collector2 = create_collector_account(
            actor=self.admin,
            username="expired_copy_col",
            email="expired_copy@example.com",
        )
        raw_token2 = self._extract_token()
        inv2 = collector2.collector_invitations.first()
        inv2.expires_at = timezone.now() - timedelta(minutes=5)
        inv2.save(update_fields=["expires_at"])
        response = self.client.get(reverse("collector_invitation_setup", kwargs={"token": raw_token2}))
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "This link has expired. Please ask your administrator to send a new invitation.", status_code=400)

        # Already used link
        collector3 = create_collector_account(
            actor=self.admin,
            username="used_copy_col",
            email="used_copy@example.com",
        )
        raw_token3 = self._extract_token()
        accept_collector_invitation(raw_token=raw_token3, password="AcceptedPass123!")
        response = self.client.get(reverse("collector_invitation_setup", kwargs={"token": raw_token3}))
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "This link has already been used. Please sign in with your password.", status_code=400)


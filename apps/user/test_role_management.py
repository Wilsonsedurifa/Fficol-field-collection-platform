from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client as TestClient, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Client
from apps.audit.models import AuditEvent
from apps.user.admin_portal.services import (
    convert_collector_to_backoffice,
    create_backoffice_user_account,
    create_collector_account,
    update_backoffice_user_account,
    update_collector_account,
)
from apps.user.models import ClientAccess, CollectorProfile, has_backoffice_access, has_backoffice_role
from apps.user.roles import (
    ROLE_FINANCE,
    ROLE_OPERATIONS,
    ROLE_SUPERVISOR,
    ROLE_SYSTEM_ADMIN,
    get_user_role_info,
    sync_backoffice_roles,
)
from apps.user.views import portal_url

User = get_user_model()


class UserRoleManagementTestCase(TestCase):
    def setUp(self):
        # Synchronize Back Office role groups and permissions
        self.role_groups = sync_backoffice_roles()
        self.operations_group = self.role_groups[ROLE_OPERATIONS]
        self.supervisor_group = self.role_groups[ROLE_SUPERVISOR]
        self.finance_group = self.role_groups[ROLE_FINANCE]
        self.sysadmin_group = self.role_groups[ROLE_SYSTEM_ADMIN]

        # Active Superuser Actor for System Admin actions
        self.admin = User.objects.create_superuser(
            username="system_admin",
            password="AdminPassword123!#",
            email="admin@example.com",
        )

        # Clients for scoping
        self.client_a = Client.objects.create(code="CLI-A", name="Client Alpha", is_active=True)
        self.client_b = Client.objects.create(code="CLI-B", name="Client Beta", is_active=True)
        self.client_inactive = Client.objects.create(code="CLI-IN", name="Client Inactive", is_active=False)

    def test_collector_creation_workflow(self):
        """Add Collector flow creates a User and a pending CollectorProfile automatically, without role groups."""
        collector = create_collector_account(
            actor=self.admin,
            username="new_collector",
            password="CollectorPass123!#",
            email="collector@example.com",
            first_name="Jane",
            last_name="Doe",
        )

        self.assertEqual(collector.username, "new_collector")
        self.assertFalse(collector.is_active)
        self.assertFalse(collector.is_superuser)
        self.assertFalse(collector.is_staff)
        self.assertEqual(collector.groups.count(), 0)

        # Collector profile created automatically
        self.assertTrue(hasattr(collector, "collector_profile"))
        profile = collector.collector_profile
        self.assertEqual(profile.approval_status, CollectorProfile.ApprovalStatus.PENDING)
        self.assertEqual(profile.availability, CollectorProfile.Availability.OFFLINE)

        # Collector invitation created automatically
        self.assertTrue(collector.collector_invitations.filter(status="sent").exists())

        # Audit event recorded
        event = AuditEvent.objects.filter(entity_type="User", entity_id=str(collector.pk), action="system_admin_user_created").first()
        self.assertIsNotNone(event)
        self.assertEqual(event.actor, self.admin)
        self.assertTrue(event.changes["collector_profile_created"])
        self.assertFalse(event.changes["is_active"])
        self.assertTrue(event.changes["invitation_sent"])

    def test_backoffice_user_creation_workflow(self):
        """Add Back Office User flow assigns role groups and client scope, without creating a CollectorProfile."""
        bo_user = create_backoffice_user_account(
            actor=self.admin,
            username="ops_user",
            password="BackOfficePass123!#",
            email="ops@example.com",
            first_name="John",
            last_name="Smith",
            role_group_ids=[self.operations_group.pk],
            selected_client_ids=[self.client_a.pk, self.client_b.pk, self.client_inactive.pk],
        )

        self.assertEqual(bo_user.username, "ops_user")
        self.assertTrue(bo_user.is_active)
        self.assertFalse(bo_user.is_superuser)
        self.assertFalse(bo_user.is_staff)

        # Assigned role group
        self.assertTrue(bo_user.groups.filter(pk=self.operations_group.pk).exists())
        self.assertEqual(bo_user.groups.count(), 1)

        # No collector profile created
        self.assertFalse(CollectorProfile.objects.filter(user=bo_user).exists())

        # Client access created only for active clients
        self.assertEqual(bo_user.client_accesses.filter(is_active=True).count(), 2)
        self.assertTrue(bo_user.client_accesses.filter(client=self.client_a, is_active=True).exists())
        self.assertTrue(bo_user.client_accesses.filter(client=self.client_b, is_active=True).exists())
        self.assertFalse(bo_user.client_accesses.filter(client=self.client_inactive).exists())

        # Audit event recorded
        event = AuditEvent.objects.filter(entity_type="User", entity_id=str(bo_user.pk), action="system_admin_user_created").first()
        self.assertIsNotNone(event)
        self.assertFalse(event.changes["collector_profile_created"])
        self.assertEqual(event.changes["role_group_count"], 1)

    def test_role_group_based_authorization(self):
        """Users assigned to Back Office role groups inherit their role permissions."""
        ops_user = create_backoffice_user_account(
            actor=self.admin,
            username="ops_worker",
            password="OpsPassword123!#",
            role_group_ids=[self.operations_group.pk],
        )
        self.assertTrue(ops_user.has_perm("accounts.validate_account_information"))
        self.assertTrue(ops_user.has_perm("activities.change_followuptask"))
        self.assertTrue(ops_user.has_perm("accounts.change_account_status"))
        self.assertFalse(ops_user.has_perm("user.approve_collector"))
        self.assertFalse(ops_user.has_perm("collection.manage_payouts"))

        sup_user = create_backoffice_user_account(
            actor=self.admin,
            username="sup_worker",
            password="SupPassword123!#",
            role_group_ids=[self.supervisor_group.pk],
        )
        self.assertTrue(sup_user.has_perm("user.view_collector_monitoring"))
        self.assertTrue(sup_user.has_perm("user.approve_collector"))
        self.assertTrue(sup_user.has_perm("assignments.manage_account_assignments"))
        self.assertTrue(sup_user.has_perm("assignments.review_release_requests"))
        self.assertFalse(sup_user.has_perm("collection.manage_payouts"))

        fin_user = create_backoffice_user_account(
            actor=self.admin,
            username="fin_worker",
            password="FinPassword123!#",
            role_group_ids=[self.finance_group.pk],
        )
        self.assertTrue(fin_user.has_perm("collection.review_commissions"))
        self.assertTrue(fin_user.has_perm("collection.manage_payouts"))
        self.assertTrue(fin_user.has_perm("collection.change_payment"))
        self.assertFalse(fin_user.has_perm("user.approve_collector"))

    def test_direct_permission_does_not_reroute_collector(self):
        """A collector with an exceptional direct permission is NOT silently rerouted to Back Office."""
        collector = create_collector_account(
            actor=self.admin,
            username="field_collector",
        )
        collector.is_active = True
        collector.save()

        # Approve the collector profile
        profile = collector.collector_profile
        profile.approval_status = CollectorProfile.ApprovalStatus.APPROVED
        profile.approved_by = self.admin
        profile.approved_at = timezone.now()
        profile.save()

        # Grant exceptional direct permission (e.g. collection.change_payment)
        perm = Permission.objects.get(codename="change_payment", content_type__app_label="collection")
        collector.user_permissions.add(perm)

        # Refresh user permissions cache
        collector = User.objects.get(pk=collector.pk)

        # Verify direct permission does not grant Back Office role or Back Office portal access
        self.assertFalse(has_backoffice_role(collector))
        self.assertFalse(has_backoffice_access(collector))

        # Portal routing routes to Collector Dashboard, NOT Back Office
        destination = portal_url(collector)
        self.assertEqual(destination, reverse("collector_dashboard"))

    def test_backoffice_role_group_portal_routing(self):
        """A user assigned to a Back Office role group routes to Back Office supervisor dashboard."""
        bo_user = create_backoffice_user_account(
            actor=self.admin,
            username="bo_portal_user",
            password="BOPassword123!#",
            role_group_ids=[self.supervisor_group.pk],
        )

        self.assertTrue(has_backoffice_role(bo_user))
        self.assertTrue(has_backoffice_access(bo_user))
        self.assertEqual(portal_url(bo_user), reverse("supervisor_dashboard"))

    def test_pending_collector_has_no_portal_access(self):
        """A pending collector has no portal access and raises PermissionDenied on portal_url."""
        collector = create_collector_account(
            actor=self.admin,
            username="pending_col",
            password="PendingPass123!#",
        )
        with self.assertRaises(PermissionDenied):
            portal_url(collector)

    def test_prevention_of_active_dual_roles_on_collector_approval(self):
        """An admin cannot approve a collector profile for a user assigned to Back Office role groups."""
        user = create_backoffice_user_account(
            actor=self.admin,
            username="dual_role_candidate",
            password="DualPass123!#",
            role_group_ids=[self.operations_group.pk],
        )
        # Create a pending profile on this user (e.g. historical or manually)
        CollectorProfile.objects.create(
            user=user,
            approval_status=CollectorProfile.ApprovalStatus.PENDING,
            availability=CollectorProfile.Availability.OFFLINE,
        )

        # Attempting to approve collector profile while holding Back Office role group must be rejected
        with self.assertRaises(ValidationError) as ctx:
            update_collector_account(
                actor=self.admin,
                user_id=user.pk,
                is_active=True,
                collector_status=CollectorProfile.ApprovalStatus.APPROVED,
            )
        self.assertIn("mutually exclusive", str(ctx.exception))

    def test_prevention_of_active_dual_roles_on_backoffice_assignment(self):
        """An admin cannot assign Back Office role groups to a user with an active approved collector profile."""
        collector = create_collector_account(
            actor=self.admin,
            username="active_approved_col",
            password="ColPass123!#",
        )
        profile = collector.collector_profile
        profile.approval_status = CollectorProfile.ApprovalStatus.APPROVED
        profile.approved_by = self.admin
        profile.approved_at = timezone.now()
        profile.save()

        # Attempting to assign Back Office role groups without conversion must be rejected
        with self.assertRaises(ValidationError) as ctx:
            update_backoffice_user_account(
                actor=self.admin,
                user_id=collector.pk,
                is_active=True,
                role_group_ids=[self.supervisor_group.pk],
            )
        self.assertIn("conversion", str(ctx.exception))

    def test_explicit_conversion_from_collector_to_backoffice(self):
        """Conversion workflow suspends collector profile, assigns Back Office role groups, logs audit events, and preserves historical data."""
        collector = create_collector_account(
            actor=self.admin,
            username="promoted_collector",
            password="PromotePass123!#",
        )
        collector.is_active = True
        collector.save()
        profile = collector.collector_profile
        profile.approval_status = CollectorProfile.ApprovalStatus.APPROVED
        profile.approved_by = self.admin
        profile.approved_at = timezone.now()
        profile.save()

        # Execute explicit conversion
        converted_user = convert_collector_to_backoffice(
            actor=self.admin,
            user_id=collector.pk,
            role_group_ids=[self.operations_group.pk, self.supervisor_group.pk],
            selected_client_ids=[self.client_a.pk],
        )

        # 1. Collector profile is suspended and offline
        profile.refresh_from_db()
        self.assertEqual(profile.approval_status, CollectorProfile.ApprovalStatus.SUSPENDED)
        self.assertEqual(profile.availability, CollectorProfile.Availability.OFFLINE)
        # Approval evidence is preserved for history
        self.assertIsNotNone(profile.approved_by)
        self.assertIsNotNone(profile.approved_at)

        # 2. Back Office role groups assigned
        self.assertTrue(converted_user.groups.filter(pk=self.operations_group.pk).exists())
        self.assertTrue(converted_user.groups.filter(pk=self.supervisor_group.pk).exists())

        # 3. Client access configured
        self.assertTrue(converted_user.client_accesses.filter(client=self.client_a, is_active=True).exists())

        # 4. Audit events recorded
        status_event = AuditEvent.objects.filter(
            action="system_admin_collector_status_changed",
            entity_id=str(profile.pk),
        ).latest("created_at")
        self.assertEqual(status_event.changes["approval_status"]["before"], "approved")
        self.assertEqual(status_event.changes["approval_status"]["after"], "suspended")

        convert_event = AuditEvent.objects.filter(
            action="system_admin_collector_converted",
            entity_id=str(converted_user.pk),
        ).latest("created_at")
        self.assertEqual(convert_event.changes["collector_status"], "suspended")
        self.assertEqual(convert_event.changes["role_group_count"], 2)

        # 5. Portal routing now routes to Back Office
        self.assertTrue(has_backoffice_role(converted_user))
        self.assertTrue(has_backoffice_access(converted_user))
        self.assertEqual(portal_url(converted_user), reverse("supervisor_dashboard"))

        # Role info displays as Back Office with historical collector note
        role_info = get_user_role_info(converted_user)
        self.assertTrue(role_info["is_backoffice"])
        self.assertTrue(role_info["is_collector"])
        self.assertEqual(role_info["collector_status"], "suspended")

    def test_preservation_of_unrelated_groups_and_direct_permissions(self):
        """Updating Back Office role groups preserves unrelated groups and permissions outside Back Office scope."""
        user = create_backoffice_user_account(
            actor=self.admin,
            username="mixed_perms_user",
            password="MixedPass123!#",
            role_group_ids=[self.operations_group.pk],
        )

        # Add unrelated custom group and permission
        custom_group = Group.objects.create(name="Custom Organization Group")
        user.groups.add(custom_group)

        custom_perm = Permission.objects.get(codename="view_auditlog", content_type__app_label="audit") if Permission.objects.filter(codename="view_auditlog").exists() else Permission.objects.first()
        user.user_permissions.add(custom_perm)

        # Update Back Office role groups to Finance
        update_backoffice_user_account(
            actor=self.admin,
            user_id=user.pk,
            is_active=True,
            role_group_ids=[self.finance_group.pk],
            selected_permission_ids=[],
            selected_client_ids=[self.client_a.pk],
        )

        user.refresh_from_db()

        # Operations group was removed, Finance was added
        self.assertFalse(user.groups.filter(pk=self.operations_group.pk).exists())
        self.assertTrue(user.groups.filter(pk=self.finance_group.pk).exists())

        # Unrelated group and permission were preserved!
        self.assertTrue(user.groups.filter(pk=custom_group.pk).exists())
        self.assertTrue(user.user_permissions.filter(pk=custom_perm.pk).exists())

    def test_superuser_protection_preserved(self):
        """Superuser accounts cannot be modified, password-reset, or converted via admin portal services."""
        with self.assertRaises(PermissionDenied):
            update_collector_account(
                actor=self.admin,
                user_id=self.admin.pk,
                is_active=True,
                collector_status=CollectorProfile.ApprovalStatus.APPROVED,
            )

        with self.assertRaises(PermissionDenied):
            update_backoffice_user_account(
                actor=self.admin,
                user_id=self.admin.pk,
                is_active=True,
                role_group_ids=[self.operations_group.pk],
            )

        with self.assertRaises(PermissionDenied):
            convert_collector_to_backoffice(
                actor=self.admin,
                user_id=self.admin.pk,
                role_group_ids=[self.operations_group.pk],
            )

    def test_system_admin_ui_user_list_and_forms(self):
        """Verify the UI routes for Add Collector, Add Back Office User, and User List."""
        client = TestClient()
        client.force_login(self.admin)

        # 1. User list shows Add Collector and Add Back Office User buttons
        resp = client.get(reverse("system_admin_users"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Add Collector")
        self.assertContains(resp, "Add Back Office User")
        self.assertContains(resp, "Role")
        self.assertContains(resp, "Portal Access")

        # 2. Add Collector form page
        resp_col_create = client.get(reverse("system_admin_collector_create"))
        self.assertEqual(resp_col_create.status_code, 200)
        self.assertContains(resp_col_create, "Add Collector")
        self.assertNotContains(resp_col_create, "Back Office Role Groups")

        # 3. Add Back Office User form page
        resp_bo_create = client.get(reverse("system_admin_backoffice_user_create"))
        self.assertEqual(resp_bo_create.status_code, 200)
        self.assertContains(resp_bo_create, "Add Back Office User")
        self.assertContains(resp_bo_create, "Back Office Role Groups")
        self.assertNotContains(resp_bo_create, "Create pending Collector Profile")

        # 4. POST create collector
        resp_post_col = client.post(reverse("system_admin_collector_create"), {
            "username": "ui_collector",
            "first_name": "Test",
            "last_name": "Collector",
            "email": "uicollector@example.com",
        })
        self.assertRedirects(resp_post_col, reverse("system_admin_users"))
        self.assertTrue(User.objects.filter(username="ui_collector").exists())
        col_user = User.objects.get(username="ui_collector")
        self.assertTrue(hasattr(col_user, "collector_profile"))
        self.assertFalse(col_user.is_active)
        self.assertTrue(col_user.collector_invitations.filter(status="sent").exists())

        # 5. POST create back office user
        resp_post_bo = client.post(reverse("system_admin_backoffice_user_create"), {
            "username": "ui_backoffice",
            "password": "ValidBackoffice123!#",
            "password_confirm": "ValidBackoffice123!#",
            "first_name": "Test",
            "last_name": "Backoffice",
            "email": "uibo@example.com",
            "role_groups": [str(self.operations_group.pk)],
            "client_accesses": [str(self.client_a.pk)],
        })
        self.assertRedirects(resp_post_bo, reverse("system_admin_users"))
        self.assertTrue(User.objects.filter(username="ui_backoffice").exists())
        bo_user = User.objects.get(username="ui_backoffice")
        self.assertFalse(CollectorProfile.objects.filter(user=bo_user).exists())
        self.assertTrue(bo_user.groups.filter(pk=self.operations_group.pk).exists())

        # 6. User list renders role badge and portal type
        resp_list = client.get(reverse("system_admin_users"))
        self.assertContains(resp_list, "ui_collector")
        self.assertContains(resp_list, "ui_backoffice")
        self.assertContains(resp_list, "Pending approval")
        self.assertContains(resp_list, "Invitation sent")
        self.assertContains(resp_list, "Back Office – Operations")
        self.assertContains(resp_list, "Back Office Portal")

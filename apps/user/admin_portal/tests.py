from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Client
from apps.user.models import BACK_OFFICE_PERMISSIONS, CollectorProfile

from .forms import ClientForm, UserCreateForm, UserEditForm
from .permissions import system_admin_required
from .services import (
    create_client_record,
    create_user_account,
    update_client_record,
    update_user_account,
)
from .views import (
    system_admin_audit,
    system_admin_client_create,
    system_admin_client_edit,
    system_admin_clients,
    system_admin_dashboard,
    system_admin_user_create,
    system_admin_user_edit,
    system_admin_users,
)

User = get_user_model()


@contextmanager
def noop_atomic():
    yield


class SystemAdminPermissionTests(SimpleTestCase):
    def setUp(self):
        self.rf = RequestFactory()

    def make_user(self, is_authenticated=True, is_active=True, is_superuser=False, is_staff=False, username="testuser"):
        return SimpleNamespace(
            pk=1,
            is_authenticated=is_authenticated,
            is_active=is_active,
            is_superuser=is_superuser,
            is_staff=is_staff,
            username=username,
            get_full_name=lambda: username,
            has_perm=lambda perm: False,
        )

    def test_anonymous_redirects_to_login(self):
        routes = [
            ("system_admin_dashboard", system_admin_dashboard, {}),
            ("system_admin_users", system_admin_users, {}),
            ("system_admin_user_create", system_admin_user_create, {}),
            ("system_admin_user_edit", system_admin_user_edit, {"user_id": 1}),
            ("system_admin_clients", system_admin_clients, {}),
            ("system_admin_client_create", system_admin_client_create, {}),
            ("system_admin_client_edit", system_admin_client_edit, {"client_id": 1}),
            ("system_admin_audit", system_admin_audit, {}),
        ]
        anon = AnonymousUser()
        for name, view_fn, kwargs in routes:
            with self.subTest(route=name):
                url = reverse(name, kwargs=kwargs)
                req = self.rf.get(url)
                req.user = anon
                resp = view_fn(req, **kwargs)
                self.assertEqual(resp.status_code, 302)
                self.assertIn("/login/", resp.url)
                self.assertIn(f"next={url}", resp.url)

    def test_anonymous_redirect_preserves_query_string_safely(self):
        url = "/system-admin/users/?q=admin&page=2&filter=active"
        req = self.rf.get(url)
        req.user = AnonymousUser()
        resp = system_admin_users(req)
        self.assertEqual(resp.status_code, 302)

        parsed = urlparse(resp.url)
        qs = parse_qs(parsed.query)
        self.assertIn("next", qs)
        self.assertEqual(qs["next"][0], url)
        # Verify query parameters cannot split or overwrite login query parameters
        self.assertNotIn("page", qs)
        self.assertNotIn("filter", qs)

    def test_ordinary_collector_denied(self):
        collector = self.make_user(is_superuser=False, is_active=True)
        req = self.rf.get("/system-admin/")
        req.user = collector
        with self.assertRaises(PermissionDenied):
            system_admin_dashboard(req)

    def test_backoffice_user_denied(self):
        bo_user = self.make_user(is_superuser=False, is_active=True)
        bo_user.has_perm = lambda perm: perm in BACK_OFFICE_PERMISSIONS
        req = self.rf.get("/system-admin/")
        req.user = bo_user
        with self.assertRaises(PermissionDenied):
            system_admin_dashboard(req)

    def test_staff_alone_denied(self):
        staff = self.make_user(is_superuser=False, is_staff=True, is_active=True)
        req = self.rf.get("/system-admin/")
        req.user = staff
        with self.assertRaises(PermissionDenied):
            system_admin_dashboard(req)

    def test_inactive_superuser_denied(self):
        inactive_admin = self.make_user(is_superuser=True, is_active=False)
        req = self.rf.get("/system-admin/")
        req.user = inactive_admin
        with self.assertRaises(PermissionDenied):
            system_admin_dashboard(req)

    def test_active_superuser_allowed(self):
        active_admin = self.make_user(is_superuser=True, is_active=True)

        @system_admin_required
        def dummy_view(request):
            return HttpResponse("OK", status=200)

        req = self.rf.get("/system-admin/")
        req.user = active_admin
        resp = dummy_view(req)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"OK")


class SystemAdminMethodRestrictionTests(SimpleTestCase):
    def setUp(self):
        self.rf = RequestFactory()
        self.admin = SimpleNamespace(
            pk=1,
            is_authenticated=True,
            is_active=True,
            is_superuser=True,
            username="superadmin",
            get_full_name=lambda: "Super Admin",
        )

    def test_unsupported_methods_return_405(self):
        routes = [
            ("system_admin_dashboard", system_admin_dashboard, {}),
            ("system_admin_users", system_admin_users, {}),
            ("system_admin_user_create", system_admin_user_create, {}),
            ("system_admin_user_edit", system_admin_user_edit, {"user_id": 1}),
            ("system_admin_clients", system_admin_clients, {}),
            ("system_admin_client_create", system_admin_client_create, {}),
            ("system_admin_client_edit", system_admin_client_edit, {"client_id": 1}),
            ("system_admin_audit", system_admin_audit, {}),
        ]
        for name, view_fn, kwargs in routes:
            with self.subTest(route=name):
                url = reverse(name, kwargs=kwargs)
                req = self.rf.put(url)
                req.user = self.admin
                resp = view_fn(req, **kwargs)
                self.assertEqual(resp.status_code, 405)

    def test_get_only_routes_reject_post(self):
        get_only = [
            ("system_admin_dashboard", system_admin_dashboard, {}),
            ("system_admin_users", system_admin_users, {}),
            ("system_admin_clients", system_admin_clients, {}),
            ("system_admin_audit", system_admin_audit, {}),
        ]
        for name, view_fn, kwargs in get_only:
            with self.subTest(route=name):
                url = reverse(name, kwargs=kwargs)
                req = self.rf.post(url)
                req.user = self.admin
                resp = view_fn(req, **kwargs)
                self.assertEqual(resp.status_code, 405)


class SystemAdminFormTests(SimpleTestCase):
    @patch("apps.user.admin_portal.forms.User.objects.filter")
    def test_user_create_form_valid(self, mock_user_filter):
        mock_user_filter.return_value.exists.return_value = False
        form = UserCreateForm(
            data={
                "username": "newuser",
                "password": "StrongSecretPass2026!#",
                "password_confirm": "StrongSecretPass2026!#",
                "first_name": "New",
                "last_name": "User",
                "email": "newuser@example.com",
                "create_collector_profile": True,
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["username"], "newuser")
        self.assertTrue(form.cleaned_data["create_collector_profile"])

    @patch("apps.user.admin_portal.forms.User.objects.filter")
    def test_user_create_form_password_mismatch(self, mock_user_filter):
        mock_user_filter.return_value.exists.return_value = False
        form = UserCreateForm(
            data={
                "username": "newuser",
                "password": "StrongSecretPass2026!#",
                "password_confirm": "DifferentSecretPass456!#",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("password_confirm", form.errors)
        self.assertIn("Passwords do not match", form.errors["password_confirm"][0])

    @patch("apps.user.admin_portal.forms.User.objects.filter")
    def test_user_create_form_rejects_weak_password(self, mock_user_filter):
        mock_user_filter.return_value.exists.return_value = False
        form = UserCreateForm(
            data={
                "username": "newuser",
                "password": "123",
                "password_confirm": "123",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("password", form.errors)

    @patch("apps.user.admin_portal.forms.User.objects.filter")
    def test_user_create_form_rejects_password_similar_to_attributes(self, mock_user_filter):
        mock_user_filter.return_value.exists.return_value = False
        form = UserCreateForm(
            data={
                "username": "supersecureuser",
                "password": "supersecureuser",
                "password_confirm": "supersecureuser",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("password", form.errors)

    @patch("apps.user.admin_portal.forms.User.objects.filter")
    def test_user_create_form_duplicate_username(self, mock_user_filter):
        mock_user_filter.return_value.exists.return_value = True
        form = UserCreateForm(
            data={
                "username": "existinguser",
                "password": "StrongSecretPass2026!#",
                "password_confirm": "StrongSecretPass2026!#",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("username", form.errors)

    @patch("apps.user.admin_portal.forms.get_available_backoffice_permissions")
    @patch("apps.user.admin_portal.forms.Client.objects.filter")
    def test_user_edit_form_password_reset_validation(self, mock_clients, mock_perms):
        mock_clients.return_value.order_by.return_value = []
        mock_perms.return_value = []
        target_user = SimpleNamespace(
            pk=2,
            is_superuser=False,
            username="targetuser",
            first_name="Target",
            last_name="User",
            email="target@example.com",
        )

        # Mismatched new passwords
        form = UserEditForm(
            data={
                "is_active": True,
                "new_password": "NewSecretPassword1!#",
                "new_password_confirm": "WrongPassword2!#",
            },
            target_user=target_user,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("new_password_confirm", form.errors)

        # Weak new password
        form_weak = UserEditForm(
            data={
                "is_active": True,
                "new_password": "123",
                "new_password_confirm": "123",
            },
            target_user=target_user,
        )
        self.assertFalse(form_weak.is_valid())
        self.assertIn("new_password", form_weak.errors)

        # Valid strong password
        form_valid = UserEditForm(
            data={
                "is_active": True,
                "new_password": "NewStrongPass2026!#",
                "new_password_confirm": "NewStrongPass2026!#",
            },
            target_user=target_user,
        )
        self.assertTrue(form_valid.is_valid(), form_valid.errors)

        # Blank passwords valid (no reset requested)
        form_blank = UserEditForm(
            data={
                "is_active": True,
                "new_password": "",
                "new_password_confirm": "",
            },
            target_user=target_user,
        )
        self.assertTrue(form_blank.is_valid(), form_blank.errors)

    @patch("apps.user.admin_portal.forms.Client.objects.filter")
    def test_client_form_validation(self, mock_filter):
        mock_filter.return_value.exclude.return_value.exists.return_value = False
        mock_filter.return_value.exists.return_value = False

        form = ClientForm(data={"code": "CLI-TEST", "name": "Test Client Org", "is_active": True})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["code"], "CLI-TEST")

        # Empty code invalid
        form_empty = ClientForm(data={"code": "  ", "name": "Test Client Org"})
        self.assertFalse(form_empty.is_valid())
        self.assertIn("code", form_empty.errors)


class SystemAdminServiceTests(SimpleTestCase):
    def setUp(self):
        self.atomic_patcher = patch(
            "apps.user.admin_portal.services.transaction.atomic",
            side_effect=lambda: noop_atomic(),
        )
        self.atomic_patcher.start()

        self.ca_patcher = patch("apps.user.admin_portal.services.ClientAccess.objects")
        self.mock_ca = self.ca_patcher.start()

        self.client_patcher = patch("apps.user.admin_portal.services.Client.objects.filter")
        self.mock_client_filter = self.client_patcher.start()
        self.mock_client_filter.return_value = MagicMock()
        self.mock_client_filter.return_value.values_list.return_value = []
        self.mock_client_filter.return_value.__iter__.return_value = []

        self.prof_patcher = patch("apps.user.admin_portal.services.CollectorProfile.objects.filter")
        self.mock_prof_filter = self.prof_patcher.start()
        self.mock_prof_filter.return_value.first.return_value = None

    def tearDown(self):
        self.atomic_patcher.stop()
        self.ca_patcher.stop()
        self.client_patcher.stop()
        self.prof_patcher.stop()

    @patch("apps.user.admin_portal.services.append_audit_event")
    @patch("apps.user.admin_portal.services.User.objects.create_user")
    @patch("apps.user.admin_portal.services.CollectorProfile.objects.create")
    def test_user_creation_with_and_without_collector_profile(
        self, mock_prof_create, mock_user_create, mock_audit
    ):
        mock_user = MagicMock(pk=10, username="regularuser", is_active=True)
        mock_user_create.return_value = mock_user
        actor = MagicMock(pk=99, is_superuser=True)

        # 1. Without collector profile
        u1 = create_user_account(
            actor=actor,
            username="regularuser",
            password="pwd",
            email="reg@example.com",
            create_collector_profile=False,
        )
        mock_user_create.assert_called_once_with(
            username="regularuser",
            password="pwd",
            email="reg@example.com",
            first_name="",
            last_name="",
            is_superuser=False,
            is_staff=False,
        )
        mock_prof_create.assert_not_called()
        self.assertEqual(u1, mock_user)
        mock_audit.assert_called_once_with(
            actor=actor,
            action="system_admin_user_created",
            entity_type="User",
            entity_id=10,
            changes={"is_active": True, "collector_profile_created": False},
        )

        # 2. With collector profile
        mock_user_create.reset_mock()
        mock_prof_create.reset_mock()
        mock_audit.reset_mock()

        u2 = create_user_account(
            actor=actor,
            username="collectoruser",
            password="pwd",
            create_collector_profile=True,
        )
        mock_user_create.assert_called_once()
        mock_prof_create.assert_called_once_with(
            user=mock_user,
            approval_status=CollectorProfile.ApprovalStatus.PENDING,
            availability=CollectorProfile.Availability.OFFLINE,
        )
        mock_audit.assert_called_once_with(
            actor=actor,
            action="system_admin_user_created",
            entity_type="User",
            entity_id=10,
            changes={"is_active": True, "collector_profile_created": True},
        )

    @patch("apps.user.admin_portal.services.User.objects.select_for_update")
    def test_superuser_edit_protection(self, mock_sfu):
        superuser_mock = MagicMock(pk=1, is_superuser=True)
        mock_sfu.return_value.get.return_value = superuser_mock

        actor = MagicMock(pk=99, is_superuser=True)
        with self.assertRaises(PermissionDenied) as ctx:
            update_user_account(
                actor=actor,
                user_id=1,
                is_active=True,
                selected_client_ids=[],
                selected_permission_ids=[],
            )
        self.assertIn("Superusers are read-only", str(ctx.exception))

    @patch("apps.user.admin_portal.services.append_audit_event")
    @patch("apps.user.admin_portal.services.get_available_backoffice_permissions")
    @patch("apps.user.admin_portal.services.User.objects.select_for_update")
    def test_direct_permission_updates_preserve_unrelated_permissions_and_groups(
        self, mock_sfu, mock_bo_perms, mock_audit
    ):
        target_user = MagicMock(pk=2, is_superuser=False, is_active=True)
        mock_sfu.return_value.get.return_value = target_user

        perm1 = MagicMock(pk=101, codename="view_collector_monitoring")
        perm2 = MagicMock(pk=102, codename="manage_point_rules")
        mock_bo_perms.return_value = [perm1, perm2]

        target_user.user_permissions.filter.return_value.values_list.return_value = [101]
        target_user.client_accesses.filter.return_value.count.return_value = 0

        actor = MagicMock(pk=99, is_superuser=True)

        update_user_account(
            actor=actor,
            user_id=2,
            is_active=True,
            selected_client_ids=[],
            selected_permission_ids=[102],
        )

        target_user.user_permissions.remove.assert_called_once_with(101)
        target_user.user_permissions.add.assert_called_once_with(102)
        target_user.groups.assert_not_called()

    @patch("apps.user.admin_portal.services.get_available_backoffice_permissions")
    @patch("apps.user.admin_portal.services.User.objects.select_for_update")
    def test_client_access_soft_deactivation_and_inactive_client_protection(
        self, mock_sfu, mock_bo_perms
    ):
        target_user = MagicMock(pk=2, is_superuser=False, is_active=True)
        mock_sfu.return_value.get.return_value = target_user
        mock_bo_perms.return_value = []
        target_user.user_permissions.filter.return_value.values_list.return_value = []
        target_user.client_accesses.filter.return_value.count.return_value = 1

        active_client = MagicMock(pk=1, is_active=True)
        self.mock_client_filter.return_value.values_list.return_value = [1]
        self.mock_client_filter.return_value.__iter__.return_value = [active_client]

        access_mock = MagicMock(is_active=False)
        self.mock_ca.get_or_create.return_value = (access_mock, False)

        actor = MagicMock(pk=99, is_superuser=True)
        update_user_account(
            actor=actor,
            user_id=2,
            is_active=True,
            selected_client_ids=[1, 999],
            selected_permission_ids=[],
        )

        self.assertTrue(access_mock.is_active)
        access_mock.save.assert_called_once_with(update_fields=["is_active", "updated_at"])
        self.mock_ca.filter.return_value.exclude.return_value.update.assert_called_once_with(
            is_active=False
        )
        self.mock_ca.delete.assert_not_called()

    @patch("apps.user.admin_portal.services.append_audit_event")
    @patch("apps.user.admin_portal.services.get_available_backoffice_permissions")
    @patch("apps.user.admin_portal.services.User.objects.select_for_update")
    def test_collector_approval_evidence_recorded_and_not_erased(
        self, mock_sfu, mock_bo_perms, mock_audit
    ):
        target_user = MagicMock(pk=2, is_superuser=False, is_active=True)
        mock_sfu.return_value.get.return_value = target_user
        mock_bo_perms.return_value = []
        target_user.user_permissions.filter.return_value.values_list.return_value = []
        target_user.client_accesses.filter.return_value.count.return_value = 0

        profile = MagicMock(
            pk=10,
            approval_status=CollectorProfile.ApprovalStatus.PENDING,
            approved_by=None,
            approved_at=None,
        )
        self.mock_prof_filter.return_value.first.return_value = profile

        actor = MagicMock(pk=99, is_superuser=True)
        update_user_account(
            actor=actor,
            user_id=2,
            is_active=True,
            selected_client_ids=[],
            selected_permission_ids=[],
            collector_status=CollectorProfile.ApprovalStatus.APPROVED,
        )

        self.assertEqual(profile.approved_by, actor)
        self.assertIsNotNone(profile.approved_at)
        self.assertEqual(profile.approval_status, CollectorProfile.ApprovalStatus.APPROVED)
        profile.save.assert_called_once()

        prior_actor = actor
        prior_time = profile.approved_at
        profile.approval_status = CollectorProfile.ApprovalStatus.APPROVED
        profile.save.reset_mock()

        update_user_account(
            actor=actor,
            user_id=2,
            is_active=True,
            selected_client_ids=[],
            selected_permission_ids=[],
            collector_status=CollectorProfile.ApprovalStatus.SUSPENDED,
        )

        self.assertEqual(profile.approval_status, CollectorProfile.ApprovalStatus.SUSPENDED)
        self.assertEqual(profile.approved_by, prior_actor)
        self.assertEqual(profile.approved_at, prior_time)

    @patch("apps.user.admin_portal.services.append_audit_event")
    @patch("apps.user.admin_portal.services.User.objects.select_for_update")
    @patch("apps.user.admin_portal.services.get_available_backoffice_permissions")
    def test_password_reset_logs_strict_evidence_no_passwords(
        self, mock_bo_perms, mock_sfu, mock_audit
    ):
        target_user = MagicMock(pk=2, is_superuser=False, is_active=True)
        mock_sfu.return_value.get.return_value = target_user
        mock_bo_perms.return_value = []
        target_user.user_permissions.filter.return_value.values_list.return_value = []
        target_user.client_accesses.filter.return_value.count.return_value = 0

        actor = MagicMock(pk=99, is_superuser=True)
        update_user_account(
            actor=actor,
            user_id=2,
            is_active=True,
            selected_client_ids=[],
            selected_permission_ids=[],
            new_password="SuperSecretPassword123!",
        )

        target_user.set_password.assert_called_once_with("SuperSecretPassword123!")

        audit_call = [
            c for c in mock_audit.call_args_list if c.kwargs.get("action") == "system_admin_password_reset"
        ]
        self.assertEqual(len(audit_call), 1)
        self.assertEqual(audit_call[0].kwargs["changes"], {"password_reset": True})
        self.assertNotIn("SuperSecretPassword123!", str(audit_call[0]))

    @patch("apps.user.admin_portal.services.CollectorProfile.objects.create")
    @patch("apps.user.admin_portal.services.append_audit_event")
    @patch("apps.user.admin_portal.services.get_available_backoffice_permissions")
    @patch("apps.user.admin_portal.services.User.objects.select_for_update")
    def test_update_user_account_creating_collector_profile_audits_user_updated(
        self, mock_sfu, mock_bo_perms, mock_audit, mock_prof_create
    ):
        target_user = MagicMock(pk=2, is_superuser=False, is_active=True)
        mock_sfu.return_value.get.return_value = target_user
        mock_bo_perms.return_value = []
        target_user.user_permissions.filter.return_value.values_list.return_value = []
        target_user.client_accesses.filter.return_value.count.return_value = 0
        self.mock_prof_filter.return_value.first.return_value = None

        actor = MagicMock(pk=99, is_superuser=True)
        update_user_account(
            actor=actor,
            user_id=2,
            is_active=True,
            selected_client_ids=[],
            selected_permission_ids=[],
            create_collector_profile=True,
        )

        mock_prof_create.assert_called_once_with(
            user=target_user,
            approval_status=CollectorProfile.ApprovalStatus.PENDING,
            availability=CollectorProfile.Availability.OFFLINE,
        )
        mock_audit.assert_called_once_with(
            actor=actor,
            action="system_admin_user_updated",
            entity_type="User",
            entity_id=2,
            changes={"collector_profile_created": True},
        )

    @patch("apps.user.admin_portal.services.CollectorProfile.objects.create")
    @patch("apps.user.admin_portal.services.append_audit_event")
    @patch("apps.user.admin_portal.services.get_available_backoffice_permissions")
    @patch("apps.user.admin_portal.services.User.objects.select_for_update")
    def test_update_user_account_existing_profile_does_not_audit_profile_created(
        self, mock_sfu, mock_bo_perms, mock_audit, mock_prof_create
    ):
        target_user = MagicMock(pk=2, is_superuser=False, is_active=True)
        mock_sfu.return_value.get.return_value = target_user
        mock_bo_perms.return_value = []
        target_user.user_permissions.filter.return_value.values_list.return_value = []
        target_user.client_accesses.filter.return_value.count.return_value = 0

        existing_profile = MagicMock(
            pk=10,
            approval_status=CollectorProfile.ApprovalStatus.PENDING,
            approved_by=None,
            approved_at=None,
        )
        self.mock_prof_filter.return_value.first.return_value = existing_profile

        actor = MagicMock(pk=99, is_superuser=True)
        update_user_account(
            actor=actor,
            user_id=2,
            is_active=True,
            selected_client_ids=[],
            selected_permission_ids=[],
            collector_status=CollectorProfile.ApprovalStatus.APPROVED,
            create_collector_profile=True,
        )

        mock_prof_create.assert_not_called()
        for call_item in mock_audit.call_args_list:
            changes = call_item.kwargs.get("changes", {})
            self.assertNotIn("collector_profile_created", changes)

    @patch("apps.user.admin_portal.services.append_audit_event")
    @patch("apps.accounts.models.Client.save")
    @patch("apps.accounts.models.Client.full_clean")
    def test_client_create_and_update_validation(self, mock_clean, mock_save, mock_audit):
        actor = MagicMock(pk=99, is_superuser=True)

        # 1. Create client
        client = create_client_record(actor=actor, code="CLI-001", name="Alpha Corp", is_active=True)
        mock_clean.assert_called_once()
        mock_save.assert_called_once()
        mock_audit.assert_called_once_with(
            actor=actor,
            action="system_admin_client_created",
            entity_type="Client",
            entity_id=client.pk,
            changes={"code": "CLI-001", "is_active": True},
        )

        # 2. Update client with code, active status, and name change together
        mock_clean.reset_mock()
        mock_save.reset_mock()
        mock_audit.reset_mock()

        with patch("apps.user.admin_portal.services.Client.objects.select_for_update") as mock_sfu:
            mock_client = MagicMock(pk=5, code="CLI-001", is_active=True)
            mock_client.name = "Alpha Corp"
            mock_sfu.return_value.get.return_value = mock_client

            update_client_record(
                actor=actor,
                client_id=5,
                code="CLI-002",
                name="Alpha Updated Corp",
                is_active=False,
            )

            mock_client.full_clean.assert_called_once()
            mock_client.save.assert_called_once()
            mock_audit.assert_called_once_with(
                actor=actor,
                action="system_admin_client_updated",
                entity_type="Client",
                entity_id=5,
                changes={
                    "code": {"before": "CLI-001", "after": "CLI-002"},
                    "is_active": {"before": True, "after": False},
                    "name_changed": True,
                },
            )
            # Ensure raw name is never in changes
            self.assertNotIn("name", mock_audit.call_args.kwargs["changes"])

    @patch("apps.user.admin_portal.services.append_audit_event")
    def test_client_name_only_update_audit(self, mock_audit):
        actor = MagicMock(pk=99, is_superuser=True)
        with patch("apps.user.admin_portal.services.Client.objects.select_for_update") as mock_sfu:
            mock_client = MagicMock(pk=5, code="CLI-001", is_active=True)
            mock_client.name = "Alpha Corp"
            mock_sfu.return_value.get.return_value = mock_client

            update_client_record(
                actor=actor,
                client_id=5,
                code="CLI-001",
                name="Alpha Renamed Corp",
                is_active=True,
            )

            mock_audit.assert_called_once_with(
                actor=actor,
                action="system_admin_client_updated",
                entity_type="Client",
                entity_id=5,
                changes={"name_changed": True},
            )
            self.assertNotIn("name", mock_audit.call_args.kwargs["changes"])

    @patch("apps.user.admin_portal.services.append_audit_event")
    def test_client_no_change_creates_no_audit(self, mock_audit):
        actor = MagicMock(pk=99, is_superuser=True)
        with patch("apps.user.admin_portal.services.Client.objects.select_for_update") as mock_sfu:
            mock_client = MagicMock(pk=5, code="CLI-001", is_active=True)
            mock_client.name = "Alpha Corp"
            mock_sfu.return_value.get.return_value = mock_client

            update_client_record(
                actor=actor,
                client_id=5,
                code="CLI-001",
                name="Alpha Corp",
                is_active=True,
            )

            mock_audit.assert_not_called()


class SystemAdminTemplateRenderingTests(SimpleTestCase):
    def test_all_system_admin_templates_render_without_syntax_errors(self):
        req_user = SimpleNamespace(username="admin", first_name="Admin", is_superuser=True, get_full_name=lambda: "Admin User")
        mock_request = MagicMock(user=req_user)

        # 1. Dashboard
        ctx_dashboard = {
            "total_users": 10,
            "active_users": 8,
            "pending_collectors": 2,
            "approved_collectors": 5,
            "suspended_collectors": 1,
            "active_backoffice_users": 4,
            "active_clients": 3,
            "inactive_clients": 1,
            "recent_audit_events": [],
            "request": mock_request,
        }
        html_dashboard = render_to_string("system_admin/dashboard.html", ctx_dashboard)
        self.assertIn("Admin dashboard", html_dashboard)
        self.assertIn("Account pipeline", html_dashboard)
        self.assertIn("Recent activity", html_dashboard)

        # 2. Users list
        user1 = SimpleNamespace(
            pk=1,
            username="collector1",
            get_full_name=lambda: "Collector One",
            email="c1@example.com",
            is_active=True,
            is_superuser=False,
            collector_profile=SimpleNamespace(get_approval_status_display=lambda: "Approved"),
            backoffice_permission_count=0,
            active_client_access_count=2,
        )
        user2 = SimpleNamespace(
            pk=2,
            username="adminuser",
            get_full_name=lambda: "Admin User",
            email="admin@example.com",
            is_active=True,
            is_superuser=True,
            collector_profile=None,
            backoffice_permission_count=12,
            active_client_access_count=0,
        )
        page_obj = Paginator([user1, user2], 10).page(1)
        ctx_users = {
            "page_obj": page_obj,
            "search_query": "",
            "request": mock_request,
        }
        html_users = render_to_string("system_admin/users.html", ctx_users)
        self.assertIn("Users &amp; Access Management", html_users)
        self.assertIn("System Account", html_users)

        # 3. User form create
        ctx_user_create = {
            "form": UserCreateForm(),
            "is_create": True,
            "request": mock_request,
        }
        html_user_create = render_to_string("system_admin/user_form.html", ctx_user_create)
        self.assertIn("Create User Account", html_user_create)

        # 4. User form superuser read-only
        ctx_user_su = {
            "target_user": SimpleNamespace(username="root", get_full_name=lambda: "Root Admin", email="root@example.com", is_active=True),
            "is_superuser_account": True,
            "request": mock_request,
        }
        html_user_su = render_to_string("system_admin/user_form.html", ctx_user_su)
        self.assertIn("Protected System Account", html_user_su)

        # 5. Clients list
        client1 = SimpleNamespace(
            pk=1,
            code="CLI-001",
            name="First Client Org",
            is_active=True,
            created_at=timezone.now(),
        )
        client_page_obj = Paginator([client1], 10).page(1)
        ctx_clients = {
            "page_obj": client_page_obj,
            "search_query": "",
            "request": mock_request,
        }
        html_clients = render_to_string("system_admin/clients.html", ctx_clients)
        self.assertIn("Client Management", html_clients)

        # 6. Client form
        ctx_client_form = {
            "form": ClientForm(),
            "is_create": True,
            "request": mock_request,
        }
        html_client_form = render_to_string("system_admin/client_form.html", ctx_client_form)
        self.assertIn("Client Code", html_client_form)

        # 7. Audit log
        event1 = SimpleNamespace(
            pk=1,
            created_at=timezone.now(),
            actor=SimpleNamespace(username="admin"),
            action="system_admin_user_updated",
            entity_type="User",
            entity_id="2",
            changes={"is_active": {"before": True, "after": False}},
        )
        audit_page_obj = Paginator([event1], 10).page(1)
        ctx_audit = {
            "page_obj": audit_page_obj,
            "action_filter": "",
            "actor_filter": "",
            "admin_actions": [("system_admin_user_updated", "User Updated")],
            "request": mock_request,
        }
        html_audit = render_to_string("system_admin/audit_log.html", ctx_audit)
        self.assertIn("Administrative Audit Log", html_audit)

    @patch("apps.user.admin_portal.forms.Client.objects.filter")
    @patch("apps.user.admin_portal.forms.get_available_backoffice_permissions")
    def test_user_edit_form_invalid_submission_retains_submitted_checkboxes(
        self, mock_bo_perms, mock_client_filter
    ):
        mock_client_filter.return_value.order_by.return_value = [
            MagicMock(pk=10, name="Client Ten", code="C10"),
            MagicMock(pk=20, name="Client Twenty", code="C20"),
        ]
        p1 = MagicMock(pk=101, name="BO Perm 1", codename="perm_1", content_type=MagicMock(app_label="app1"))
        p2 = MagicMock(pk=102, name="BO Perm 2", codename="perm_2", content_type=MagicMock(app_label="app2"))
        mock_bo_perms.return_value = [p1, p2]

        target_user = SimpleNamespace(
            pk=5,
            username="testuser",
            get_full_name=lambda: "Test User",
            email="test@example.com",
            is_active=True,
            is_superuser=False,
            collector_profile=None,
        )
        mock_request = MagicMock(user=SimpleNamespace(username="admin", is_superuser=True))

        form = UserEditForm(
            data={
                "is_active": True,
                "client_accesses": ["10"],
                "backoffice_permissions": ["102"],
                "new_password": "ValidPass123!#",
                "new_password_confirm": "MismatchPass123!#",
            },
            target_user=target_user,
        )
        self.assertFalse(form.is_valid())

        ctx = {
            "form": form,
            "target_user": target_user,
            "is_create": False,
            "admin_section": "users",
            "has_collector_profile": False,
            "request": mock_request,
        }
        html = render_to_string("system_admin/user_form.html", ctx)

        self.assertIn('name="client_accesses" value="10" checked', html)
        self.assertIn('name="client_accesses" value="20"', html)
        self.assertNotIn('name="client_accesses" value="20" checked', html)

        self.assertIn('name="backoffice_permissions" value="102" checked', html)
        self.assertIn('name="backoffice_permissions" value="101"', html)
        self.assertNotIn('name="backoffice_permissions" value="101" checked', html)

    @patch("apps.user.admin_portal.forms.Client.objects.filter")
    @patch("apps.user.admin_portal.forms.get_available_backoffice_permissions")
    def test_user_edit_form_unbound_get_renders_initial_checkboxes_checked(
        self, mock_bo_perms, mock_client_filter
    ):
        mock_client_filter.return_value.order_by.return_value = [
            MagicMock(pk=10, name="Client Ten", code="C10"),
            MagicMock(pk=20, name="Client Twenty", code="C20"),
        ]
        p1 = MagicMock(pk=101, name="BO Perm 1", codename="perm_1", content_type=MagicMock(app_label="app1"))
        p2 = MagicMock(pk=102, name="BO Perm 2", codename="perm_2", content_type=MagicMock(app_label="app2"))
        mock_bo_perms.return_value = [p1, p2]

        target_user = SimpleNamespace(
            pk=5,
            username="testuser",
            get_full_name=lambda: "Test User",
            email="test@example.com",
            is_active=True,
            is_superuser=False,
            collector_profile=None,
        )
        mock_request = MagicMock(user=SimpleNamespace(username="admin", is_superuser=True))

        form = UserEditForm(
            initial={
                "is_active": True,
                "client_accesses": ["20"],
                "backoffice_permissions": ["101"],
            },
            target_user=target_user,
        )
        self.assertFalse(form.is_bound)

        ctx = {
            "form": form,
            "target_user": target_user,
            "is_create": False,
            "admin_section": "users",
            "has_collector_profile": False,
            "request": mock_request,
        }
        html = render_to_string("system_admin/user_form.html", ctx)

        self.assertIn('name="client_accesses" value="20" checked', html)
        self.assertIn('name="client_accesses" value="10"', html)
        self.assertNotIn('name="client_accesses" value="10" checked', html)

        self.assertIn('name="backoffice_permissions" value="101" checked', html)
        self.assertIn('name="backoffice_permissions" value="102"', html)
        self.assertNotIn('name="backoffice_permissions" value="102" checked', html)

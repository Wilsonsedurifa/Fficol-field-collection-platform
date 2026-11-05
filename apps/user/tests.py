from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch, MagicMock
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Group, Permission
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone
from apps.assignments import views as assignment_views
from apps.assignments.services import collector_profile, submit_collector_location, OfferWorkflowError, LocationSubmissionError
from apps.assignments.tests import observation
from apps.activities import views as activity_views
from .models import CollectorProfile, has_backoffice_access
from . import views


class PortalSeparationTests(SimpleTestCase):
    def user(self, superuser=False, permissions=(), groups=(), active=True, staff=False):
        return SimpleNamespace(
            pk=1,
            is_authenticated=True,
            is_active=active,
            is_superuser=superuser,
            is_staff=staff,
            groups=groups,
            has_perm=lambda name: name in permissions,
        )

    def request(self, user, method='get', body='{}', url='/collector/'):
        request = RequestFactory().generic(method.upper(), url, data=body, content_type='application/json')
        request.user = user
        request._dont_enforce_csrf_checks = True
        return request

    def test_login_next_url_cannot_skip_role_routing(self):
        view = views.FiColLoginView()
        view.request = RequestFactory().get('/login/?next=/collector/dashboard/')
        view.request.user = self.user(superuser=True)
        self.assertEqual(view.get_success_url(), reverse('system_admin_dashboard'))

    def test_superuser_and_role_groups_take_precedence(self):
        self.assertTrue(has_backoffice_access(self.user(superuser=True)))
        from apps.user.roles import PRIMARY_ROLE_NAMES
        for role_name in PRIMARY_ROLE_NAMES:
            self.assertTrue(has_backoffice_access(self.user(groups=[SimpleNamespace(name=role_name)])))
        # Direct permissions alone do NOT grant Back Office access
        for permission in [
            'activities.change_fieldactivity',
            'activities.change_followuptask',
            'collection.manage_earning_rules',
            'collection.review_commissions',
            'collection.manage_payouts',
            'collection.manage_point_rules',
        ]:
            self.assertFalse(has_backoffice_access(self.user(permissions=[permission])))
        self.assertFalse(has_backoffice_access(self.user(staff=True)))
        self.assertFalse(has_backoffice_access(self.user(superuser=True, active=False)))
        self.assertFalse(has_backoffice_access(AnonymousUser()))

    def test_privileged_login_does_not_query_or_route_to_collector_profile(self):
        from apps.user.roles import ROLE_OPERATIONS, ROLE_SUPERVISOR
        for user in [
            self.user(groups=[SimpleNamespace(name=ROLE_OPERATIONS)]),
            self.user(groups=[SimpleNamespace(name=ROLE_SUPERVISOR)]),
        ]:
            with patch('apps.user.views.CollectorProfile.objects') as profiles:
                destination = views.portal_url(user)
                profiles.filter.assert_not_called()
            self.assertEqual(destination, reverse('supervisor_dashboard'))
        with patch('apps.user.views.CollectorProfile.objects') as profiles:
            destination = views.portal_url(self.user(superuser=True))
            profiles.filter.assert_not_called()
        self.assertEqual(destination, reverse('system_admin_dashboard'))

    def test_collector_with_direct_permission_not_rerouted_to_backoffice(self):
        """A collector holding exceptional direct permissions is not silently rerouted."""
        user = self.user(permissions=['activities.change_followuptask'])
        with patch('apps.user.views.CollectorProfile.objects') as profiles:
            profiles.filter.return_value.exists.return_value = True
            self.assertEqual(views.portal_url(user), reverse('collector_dashboard'))

    def test_only_approved_ordinary_collector_routes_to_collector_dashboard(self):
        user=self.user()
        with patch('apps.user.views.CollectorProfile.objects') as profiles:
            profiles.filter.return_value.exists.return_value=True
            self.assertEqual(views.portal_url(user),reverse('collector_dashboard'))
            profiles.filter.assert_called_once_with(user=user,approval_status='approved')
            profiles.filter.return_value.exists.return_value=False
            with self.assertRaises(PermissionDenied):
                views.portal_url(user)

    def test_privileged_get_pages_block_before_any_collector_lookup(self):
        from apps.collection.views import collector_commissions, collector_performance
        user=self.user(superuser=True)
        with patch('apps.user.views.CollectorProfile.objects') as profiles:
            with self.assertRaises(PermissionDenied):views.collector_dashboard(self.request(user))
            profiles.filter.assert_not_called()
        for view in [assignment_views.account_offers_page,assignment_views.my_accounts_page,activity_views.followups_page,collector_commissions,collector_performance]:
            with self.assertRaises(PermissionDenied):view(self.request(user))
        with self.assertRaises(PermissionDenied):assignment_views.account_detail_page(self.request(user),assignment_id=1)

    def test_privileged_api_calls_are_denied_before_services(self):
        user=self.user(superuser=True)
        with patch('apps.assignments.views.submit_collector_location') as service:
            response=assignment_views.submit_location(self.request(user,'post'))
            self.assertEqual(response.status_code,403);service.assert_not_called()
        with patch('apps.assignments.views.offers_state') as service:
            self.assertEqual(assignment_views.offers_list(self.request(user)).status_code,403)
            service.assert_not_called()
        for view,kwargs in [(assignment_views.offers_refresh,{}),(assignment_views.availability,{}),(assignment_views.accept_offer,{'offer_id':1}),(assignment_views.decline_offer,{'offer_id':1})]:
            self.assertEqual(view(self.request(user,'post'),**kwargs).status_code,403)
        for view,kwargs in [(activity_views.start,{'assignment_id':1}),(activity_views.submit,{'assignment_id':1,'activity_id':1}),(activity_views.cancel,{'assignment_id':1,'activity_id':1}),(activity_views.schedule,{'assignment_id':1,'activity_id':1})]:
            self.assertEqual(view(self.request(user,'post'),**kwargs).status_code,403)

    def test_service_guards_reject_retained_approved_collector_profile(self):
        profile=SimpleNamespace(user=self.user(superuser=True),approval_status='approved')
        with patch('apps.assignments.services.CollectorProfile.objects') as records:
            records.select_related.return_value.filter.return_value.first.return_value=profile
            with self.assertRaises(OfferWorkflowError) as caught:collector_profile(1)
            self.assertEqual(caught.exception.code,'collector_portal_forbidden')

    def test_ordinary_collector_cannot_enter_backoffice_by_url(self):
        from apps.collection.views import backoffice_point_rules
        user=self.user()
        for view in [activity_views.review_dashboard,activity_views.backoffice_visits,activity_views.backoffice_followups,backoffice_point_rules]:
            with self.assertRaises(PermissionDenied):view(self.request(user))


class PortalSeparationDatabaseTests(TestCase):
    def setUp(self):
        self.user=get_user_model().objects.create_user(username='portal-role-test', password='portal-test-password')
        self.profile=CollectorProfile.objects.create(user=self.user,approval_status='approved',approved_by=self.user,approved_at=timezone.now())
        self.client.force_login(self.user)

    def login(self):
        return self.client.post(reverse('login'), {'username': 'portal-role-test', 'password': 'portal-test-password'})

    def test_login_form_is_available_to_signed_in_user_without_portal_access(self):
        CollectorProfile.objects.filter(pk=self.profile.pk).update(approval_status='pending')
        response = self.client.get(reverse('login'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'registration/login.html')

    def test_promoting_collector_to_backoffice_changes_next_request_and_preserves_profile(self):
        self.assertRedirects(self.login(),reverse('collector_dashboard'))
        group=Group.objects.create(name='Portal scheduling test')
        group.permissions.add(Permission.objects.get(codename='change_followuptask',content_type__app_label='activities'))
        self.user.groups.add(group)
        self.assertRedirects(self.login(),reverse('supervisor_dashboard'))
        self.assertEqual(self.client.get(reverse('collector_dashboard')).status_code,403)
        self.assertEqual(self.client.get(reverse('assignments:account_offers')).status_code,403)
        self.assertEqual(self.client.post(reverse('assignments:submit_location'),data='{}',content_type='application/json').status_code,403)
        with self.assertRaises(OfferWorkflowError):collector_profile(self.user.pk)
        with self.assertRaises(LocationSubmissionError) as caught:submit_collector_location(self.user.pk,observation())
        self.assertEqual(caught.exception.code,'collector_portal_forbidden')
        self.assertTrue(CollectorProfile.objects.filter(pk=self.profile.pk,approval_status='approved').exists())
        self.user.groups.clear()
        self.assertRedirects(self.login(),reverse('collector_dashboard'))

    def test_superuser_with_profile_cannot_use_collector_portal(self):
        get_user_model().objects.filter(pk=self.user.pk).update(is_superuser=True,is_staff=True)
        self.assertRedirects(self.login(),reverse('system_admin_dashboard'))
        self.assertEqual(self.client.get(reverse('assignments:my_accounts')).status_code,403)
        self.assertEqual(self.client.get(reverse('activities:followups')).status_code,403)
        self.assertEqual(self.client.post(reverse('activities:start_visit',args=[1]),data='{}',content_type='application/json').status_code,403)

    def test_pending_collector_has_no_portal_access(self):
        CollectorProfile.objects.filter(pk=self.profile.pk).update(approval_status='pending')
        self.assertRedirects(self.login(), reverse('collector_pending_approval'))
        self.assertRedirects(self.client.get(reverse('collector_dashboard')), reverse('collector_pending_approval'))
        self.assertEqual(self.client.get(reverse('assignments:account_offers')).status_code, 403)
        self.assertEqual(self.client.get(reverse('activities:followups')).status_code, 403)


class PortalOfferExclusionTests(TestCase):
    def test_nearest_backoffice_profiles_are_excluded_before_recipient_limit(self):
        from apps.assignments.tests import offer_fixture
        from apps.assignments.models import CollectorLocation
        from apps.assignments.services import _candidate_profiles
        from .models import ClientAccess
        user,profile,client,account=offer_fixture()
        permission=Permission.objects.get(codename='change_followuptask',content_type__app_label='activities')
        group=Group.objects.create(name='Offer exclusion group')
        group.permissions.add(permission)
        privileged=[]
        for name,superuser in [('offer-superuser',True),('offer-group-scheduler',False)]:
            other=get_user_model().objects.create_user(username=name,is_superuser=superuser)
            if not superuser:other.groups.add(group)
            other_profile=CollectorProfile.objects.create(user=other,approval_status='approved',approved_by=user,approved_at=timezone.now(),availability='online')
            CollectorLocation.objects.create(collector=other_profile,position=account.collection_address.position,accuracy_m=5,captured_at=timezone.now(),received_at=timezone.now())
            ClientAccess.objects.create(user=other,client=client)
            privileged.append(other_profile.pk)
        candidates=_candidate_profiles(account,client,timezone.now())
        self.assertIn(profile.pk,candidates)
        for profile_id in privileged:self.assertNotIn(profile_id,candidates)


class CollectorProfileAndHelpTests(SimpleTestCase):
    def user(self, superuser=False, permissions=(), groups=(), active=True, staff=False, username="collector_test", pk=1):
        return SimpleNamespace(
            pk=pk,
            is_authenticated=True,
            is_active=active,
            is_superuser=superuser,
            is_staff=staff,
            groups=groups,
            has_perm=lambda name: name in permissions,
            get_full_name=lambda: "Test Collector",
            username=username,
            email="collector@example.com",
            first_name="Test",
        )

    def request(self, user, method='get', body='{}', url='/collector/'):
        request = RequestFactory().generic(method.upper(), url, data=body, content_type='application/json')
        request.user = user
        request._dont_enforce_csrf_checks = True
        return request

    def test_anonymous_user_redirected_to_login(self):
        for path_name in ['collector_profile', 'collector_help']:
            url = reverse(path_name)
            request = self.request(AnonymousUser(), url=url)
            view_fn = views.collector_profile if path_name == 'collector_profile' else views.collector_help
            response = view_fn(request)
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.url.startswith('/login/'))
            self.assertIn(url, response.url)

    def test_backoffice_users_denied_before_collector_lookup(self):
        from apps.user.roles import ROLE_OPERATIONS, ROLE_SUPERVISOR
        privileged_users = [
            self.user(superuser=True),
            self.user(groups=[SimpleNamespace(name=ROLE_OPERATIONS)]),
            self.user(groups=[SimpleNamespace(name=ROLE_SUPERVISOR)]),
        ]
        for privileged in privileged_users:
            with patch('apps.user.views.CollectorProfile.objects') as mock_profiles:
                with self.assertRaises(PermissionDenied):
                    views.collector_profile(self.request(privileged))
                with self.assertRaises(PermissionDenied):
                    views.collector_help(self.request(privileged))
                mock_profiles.filter.assert_not_called()

    def test_inactive_user_denied(self):
        inactive = self.user(active=False)
        with self.assertRaises(PermissionDenied):
            views.collector_profile(self.request(inactive))
        with self.assertRaises(PermissionDenied):
            views.collector_help(self.request(inactive))

    def test_unapproved_collector_denied(self):
        user = self.user()
        with patch('apps.user.views.CollectorProfile.objects.filter') as mock_filter:
            mock_filter.return_value.first.return_value = None
            with self.assertRaises(PermissionDenied):
                views.collector_profile(self.request(user))
            with self.assertRaises(PermissionDenied):
                views.collector_help(self.request(user))

    def test_approved_collector_can_access_both_pages(self):
        user = self.user()
        profile = SimpleNamespace(
            user=user,
            approval_status="approved",
            availability="online",
            matching_radius_m=5000,
            approved_at=None,
            get_approval_status_display=lambda: "Approved",
            get_availability_display=lambda: "Online",
        )
        with patch('apps.user.views.CollectorProfile.objects.filter') as mock_prof_filter, \
             patch('apps.user.views.ClientAccess.objects.filter') as mock_client_filter, \
             patch('apps.user.views.render') as mock_render:
            mock_prof_filter.return_value.first.return_value = profile
            mock_client_filter.return_value.select_related.return_value.order_by.return_value = []
            mock_render.return_value = HttpResponse(b"OK", status=200)

            resp_profile = views.collector_profile(self.request(user))
            self.assertEqual(resp_profile.status_code, 200)
            self.assertEqual(mock_render.call_args_list[0][0][1], "collector/profile.html")
            self.assertEqual(
                mock_render.call_args_list[0][0][2],
                {
                    "collector_profile": profile,
                    "collector_section": "profile",
                    "navbar_title": "Profile",
                    "client_accesses": [],
                },
            )

            resp_help = views.collector_help(self.request(user))
            self.assertEqual(resp_help.status_code, 200)
            self.assertEqual(mock_render.call_args_list[1][0][1], "collector/help_support.html")
            self.assertEqual(
                mock_render.call_args_list[1][0][2],
                {
                    "collector_profile": profile,
                    "collector_section": "help",
                    "navbar_title": "Help & Support",
                },
            )

    def test_profile_context_limited_to_signed_in_collector_and_active_client_access(self):
        user = self.user(pk=99)
        profile = SimpleNamespace(
            user=user,
            approval_status="approved",
            availability="online",
            matching_radius_m=5000,
            approved_at=None,
            get_approval_status_display=lambda: "Approved",
            get_availability_display=lambda: "Online",
        )
        dummy_grants = [SimpleNamespace(client=SimpleNamespace(name="Client A", code="CA"), is_active=True)]

        with patch('apps.user.views.CollectorProfile.objects.filter') as mock_prof_filter, \
             patch('apps.user.views.ClientAccess.objects.filter') as mock_client_filter, \
             patch('apps.user.views.render') as mock_render:
            mock_prof_filter.return_value.first.return_value = profile
            mock_client_filter.return_value.select_related.return_value.order_by.return_value = dummy_grants
            mock_render.return_value = HttpResponse(b"OK", status=200)

            views.collector_profile(self.request(user))

            mock_prof_filter.assert_called_once_with(user=user, approval_status="approved")
            mock_client_filter.assert_called_once_with(user=user, is_active=True, client__is_active=True)
            context = mock_render.call_args[0][2]
            self.assertEqual(context["collector_profile"], profile)
            self.assertEqual(context["client_accesses"], dummy_grants)
            self.assertEqual(context["collector_section"], "profile")
            self.assertEqual(context["navbar_title"], "Profile")

    def test_post_requests_rejected_on_both_routes(self):
        user = self.user()
        profile = SimpleNamespace(user=user, approval_status="approved")
        with patch('apps.user.views.CollectorProfile.objects.filter') as mock_prof_filter:
            mock_prof_filter.return_value.first.return_value = profile
            resp_profile = views.collector_profile(self.request(user, method='post'))
            self.assertEqual(resp_profile.status_code, 405)
            resp_help = views.collector_help(self.request(user, method='post'))
            self.assertEqual(resp_help.status_code, 405)

    def test_templates_render_without_syntax_errors(self):
        from django.template.loader import render_to_string
        context = {
            "collector_profile": SimpleNamespace(
                approval_status="approved",
                availability="online",
                matching_radius_m=5000,
                approved_at=timezone.now(),
                get_approval_status_display=lambda: "Approved",
                get_availability_display=lambda: "Online",
            ),
            "collector_section": "profile",
            "navbar_title": "Profile",
            "client_accesses": [],
            "request": self.request(self.user()),
        }
        rendered_profile = render_to_string("collector/profile.html", context)
        self.assertIn("Field Collector", rendered_profile)
        self.assertIn("Account Information", rendered_profile)

        context["collector_section"] = "help"
        context["navbar_title"] = "Help & Support"
        rendered_help = render_to_string("collector/help_support.html", context)
        self.assertIn("Collector Help &amp; Support", rendered_help)
        self.assertIn("Important Operational Policies", rendered_help)


class CollectorPerformanceViewTests(SimpleTestCase):
    def user(self, superuser=False, permissions=(), groups=(), active=True, staff=False):
        return SimpleNamespace(
            pk=1,
            is_authenticated=True,
            is_active=active,
            is_superuser=superuser,
            is_staff=staff,
            groups=groups,
            username="testuser",
            first_name="Test",
            get_full_name=lambda: "Test User",
            has_perm=lambda name: name in permissions,
        )

    def request(self, user, method='get', body='{}', url='/collector/performance/'):
        request = RequestFactory().generic(method.upper(), url, data=body, content_type='application/json')
        request.user = user
        request._dont_enforce_csrf_checks = True
        return request

    def test_performance_blocks_privileged_backoffice_users(self):
        from apps.collection.views import collector_performance
        from apps.user.roles import ROLE_FINANCE
        for privileged in [self.user(superuser=True), self.user(groups=[SimpleNamespace(name=ROLE_FINANCE)])]:
            with self.assertRaises(PermissionDenied):
                collector_performance(self.request(privileged))

    def test_performance_blocks_unapproved_collector(self):
        from apps.collection.views import collector_performance
        user = self.user()
        with patch('apps.collection.views.CollectorProfile.objects.filter') as mock_filter:
            mock_filter.return_value.first.return_value = None
            with self.assertRaises(PermissionDenied):
                collector_performance(self.request(user))

    def test_performance_approved_collector_renders_template_with_context(self):
        from apps.collection.views import collector_performance
        user = self.user()
        profile = SimpleNamespace(
            pk=1,
            user=user,
            approval_status="approved",
        )
        fake_summary = {
            "total": 5,
            "breakdown": {
                "address": {"code": "address", "label": "Address", "points": 2},
                "email": {"code": "email", "label": "Email", "points": 1},
                "phone": {"code": "phone", "label": "Phone number", "points": 1},
                "contact_person": {"code": "contact_person", "label": "Contact person", "points": 1},
                "payment_arrangement": {"code": "payment_arrangement", "label": "Payment arrangement", "points": 0},
            },
        }
        with patch('apps.collection.views.CollectorProfile.objects.filter') as mock_filter, \
             patch('apps.collection.views.collector_points_summary', return_value=fake_summary), \
             patch('apps.collection.views.collector_point_transactions_list', return_value=[]), \
             patch('apps.collection.views.render') as mock_render:
            mock_filter.return_value.first.return_value = profile
            mock_render.return_value = HttpResponse(b"OK", status=200)

            resp = collector_performance(self.request(user))
            self.assertEqual(resp.status_code, 200)
            mock_render.assert_called_once()
            context = mock_render.call_args[0][2]
            self.assertEqual(context["collector_section"], "performance")
            self.assertEqual(context["navbar_title"], "Stars & Performance")
            self.assertEqual(context["summary"], fake_summary)

    def test_collector_dashboard_includes_verified_points(self):
        user = self.user()
        profile = SimpleNamespace(
            pk=1,
            user=user,
            approval_status="approved",
            matching_radius_m=5000,
            availability="online",
            get_availability_display=lambda: "Online",
        )
        fake_points = {
            "total": 7,
            "breakdown": {},
        }
        fake_earnings = {
            "awaiting_approval": Decimal("0.00"),
            "approved_unpaid": Decimal("0.00"),
            "paid_out": Decimal("0.00"),
        }
        with patch('apps.user.views.CollectorProfile.objects.filter') as mock_filter, \
             patch('apps.collection.services.collector_points_summary', return_value=fake_points), \
             patch('apps.collection.services.collector_earnings_summary', return_value=fake_earnings), \
             patch('apps.assignments.models.AccountAssignment.objects.filter') as mock_assign, \
             patch('apps.activities.services.pending_followups', return_value=[]), \
             patch('apps.activities.services.followup_counts', return_value={"total": 0}), \
             patch('apps.assignments.services.acceptance_allowance', return_value={"used": 0, "limit": 10, "remaining": 10}), \
             patch('apps.assignments.services.client_ids', return_value=[]), \
             patch('apps.user.views.render') as mock_render:
            mock_filter.return_value.first.return_value = profile
            mock_qs = MagicMock()
            mock_qs.count.return_value = 0
            mock_qs.__getitem__.return_value = []
            mock_assign.return_value.select_related.return_value.order_by.return_value = mock_qs
            mock_render.return_value = HttpResponse(b"OK", status=200)

            resp = views.collector_dashboard(self.request(user, url='/collector/dashboard/'))
            self.assertEqual(resp.status_code, 200)
            context = mock_render.call_args[0][2]
            self.assertTrue(context["performance_connected"])
            self.assertEqual(context["verified_points"], 7)

    def test_performance_template_renders_without_syntax_errors(self):
        from django.template.loader import render_to_string
        context = {
            "collector_profile": SimpleNamespace(
                approval_status="approved",
                availability="online",
                matching_radius_m=5000,
                approved_at=timezone.now(),
                get_approval_status_display=lambda: "Approved",
                get_availability_display=lambda: "Online",
            ),
            "collector_section": "performance",
            "navbar_title": "Stars & Performance",
            "summary": {
                "total": 3,
                "breakdown": {
                    "address": {"code": "address", "label": "Address", "points": 1},
                    "email": {"code": "email", "label": "Email", "points": 1},
                    "phone": {"code": "phone", "label": "Phone number", "points": 1},
                    "contact_person": {"code": "contact_person", "label": "Contact person", "points": 0},
                    "payment_arrangement": {"code": "payment_arrangement", "label": "Payment arrangement", "points": 0},
                },
            },
            "transactions": [],
            "request": self.request(self.user()),
        }
        rendered = render_to_string("collector/performance.html", context)
        self.assertIn("Stars &amp; Performance", rendered)
        self.assertIn("Total Verified Points", rendered)
        self.assertIn("Address", rendered)
        self.assertIn("Payment Arrangement", rendered)

    def test_backoffice_point_rules_template_renders_without_syntax_errors(self):
        from django.template.loader import render_to_string
        from apps.collection.models import InformationCategory
        context = {
            "backoffice_section": "point_rules",
            "page_title": "Point Rules",
            "categories": InformationCategory.choices,
            "rules": [],
            "page_obj": SimpleNamespace(paginator=SimpleNamespace(count=0, num_pages=1), has_other_pages=False),
            "can_manage_point_rules": True,
            "request": self.request(self.user(superuser=True)),
        }
        rendered = render_to_string("backoffice/point_rules.html", context)
        self.assertIn("Point Rule Version", rendered)
        self.assertIn("Point Rule History", rendered)

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import close_old_connections, connections
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.user.models import CollectorProfile

from .models import CollectorLocation
from .services import LocationSubmissionError, submit_collector_location, submission_limits, validate_observation
from .views import submit_location


def observation(now=None, **changes):
    now = now or timezone.now()
    payload = {
        "longitude": 120.9842, "latitude": 14.5995,
        "accuracy_m": 18.25, "captured_at": now.isoformat(),
    }
    payload.update(changes)
    return payload


GPS_LIMITS = {
    "LOCATION_SUBMISSION_MAX_AGE_SECONDS": 300,
    "LOCATION_SUBMISSION_MAX_ACCURACY_M": 50,
    "LOCATION_SUBMISSION_MAX_CLOCK_SKEW_SECONDS": 120,
}


@override_settings(**GPS_LIMITS)
class LocationValidationTests(SimpleTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.clock = patch("apps.assignments.services.timezone.now", return_value=self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def assert_error(self, payload, code):
        with self.assertRaises(LocationSubmissionError) as caught:
            validate_observation(payload)
        self.assertEqual(caught.exception.code, code)

    def test_valid_fix_and_conservative_accuracy_rounding(self):
        result = validate_observation(observation(self.now, accuracy_m=18.251))
        self.assertEqual(result.accuracy_m, Decimal("18.26"))
        self.assertEqual((result.longitude, result.latitude), (120.9842, 14.5995))

    def test_rejects_missing_extra_and_spoofed_identity_fields(self):
        for payload in [None, [], {}, {**observation(self.now), "collector_id": 99}]:
            with self.subTest(payload=payload):
                self.assert_error(payload, "invalid_observation")

    def test_rejects_non_numeric_and_non_finite_numbers(self):
        for field in ["latitude", "longitude", "accuracy_m"]:
            for value in [True, None, "14.5", float("nan"), float("inf")]:
                with self.subTest(field=field, value=value):
                    self.assert_error(observation(self.now, **{field: value}), "invalid_observation")

    def test_rejects_out_of_range_and_negative_accuracy(self):
        for changes in [{"longitude": 181}, {"latitude": -91}, {"accuracy_m": -0.01}]:
            with self.subTest(changes=changes):
                self.assert_error(observation(self.now, **changes), "invalid_observation")

    def test_checks_accuracy_before_rounding(self):
        self.assert_error(observation(self.now, accuracy_m=50.001), "inaccurate_location")
        validate_observation(observation(self.now, accuracy_m=50))

    def test_rejects_invalid_and_naive_timestamps(self):
        for value in [None, 123, "2026-02-30T12:00:00Z", "not-a-date", "2026-10-12T12:00:00"]:
            with self.subTest(value=value):
                self.assert_error(observation(self.now, captured_at=value), "invalid_observation")

    def test_age_and_future_skew_boundaries(self):
        validate_observation(observation(self.now - timedelta(seconds=300)))
        validate_observation(observation(self.now + timedelta(seconds=120)))
        self.assert_error(observation(self.now - timedelta(seconds=301)), "stale_location")
        self.assert_error(observation(self.now + timedelta(seconds=121)), "future_location")

    def test_limits_are_configurable_and_invalid_limits_fail_closed(self):
        with override_settings(LOCATION_SUBMISSION_MAX_ACCURACY_M=10):
            self.assert_error(observation(self.now), "inaccurate_location")
        for value in [0, -1, float("nan"), "bad", 1000000]:
            with self.subTest(value=value), override_settings(LOCATION_SUBMISSION_MAX_ACCURACY_M=value):
                with self.assertRaises(ImproperlyConfigured):
                    submission_limits()


class LocationEndpointTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.url = reverse("assignments:submit_location")
        self.user = SimpleNamespace(pk=7, is_authenticated=True, is_active=True)

    def request(self, body=None, method="POST", content_type="application/json", csrf=True):
        request = self.factory.generic(method, self.url, body or json.dumps(observation()), content_type=content_type)
        request.user = self.user
        request._dont_enforce_csrf_checks = csrf
        return request

    def test_route_and_existing_routes_remain_available(self):
        self.assertEqual(self.url, "/collector/location/")
        self.assertEqual(reverse("collector_dashboard"), "/collector/dashboard/")
        self.assertEqual(reverse("login"), "/login/")
        self.assertEqual(reverse("logout"), "/logout/")

    def test_requires_post(self):
        response = submit_location(self.request(method="GET"))
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response["Allow"], "POST")

    def test_requires_authentication_and_active_user(self):
        request = self.request()
        request.user = AnonymousUser()
        self.assertEqual(submit_location(request).status_code, 401)
        self.user.is_active = False
        self.assertEqual(submit_location(self.request()).status_code, 403)

    def test_csrf_is_not_exempt(self):
        with patch("apps.assignments.views.submit_collector_location") as service:
            response = submit_location(self.request(csrf=False))
        self.assertEqual(response.status_code, 403)
        service.assert_not_called()

    def test_requires_json_and_bounded_body(self):
        self.assertEqual(submit_location(self.request(content_type="text/plain")).status_code, 415)
        self.assertEqual(submit_location(self.request(body="x" * 4097)).status_code, 413)

    def test_rejects_malformed_duplicate_and_non_standard_json(self):
        for body in ['{', '{"latitude": 1, "latitude": 2}', '{"latitude": NaN}', b'\xff']:
            with self.subTest(body=body), patch("apps.assignments.views.submit_collector_location") as service:
                response = submit_location(self.request(body=body))
                self.assertEqual(response.status_code, 400)
                service.assert_not_called()

    def test_passes_authenticated_identity_and_returns_service_response(self):
        expected = {"saved": True, "changed": True, "matching_eligibility": "not_evaluated"}
        with patch("apps.assignments.views.submit_collector_location", return_value=expected) as service:
            response = submit_location(self.request())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content), expected)
        self.assertEqual(service.call_args.args[0], self.user.pk)
        self.assertIn("no-store", response["Cache-Control"])

    def test_returns_controlled_service_errors(self):
        for code, status in [("approval_required", 403), ("older_observation", 409), ("stale_location", 400)]:
            with self.subTest(code=code), patch(
                "apps.assignments.views.submit_collector_location",
                side_effect=LocationSubmissionError(code, "Please try again.", status),
            ):
                response = submit_location(self.request())
                self.assertEqual(response.status_code, status)
                self.assertEqual(json.loads(response.content)["error"]["code"], code)


def approved_collector():
    User = get_user_model()
    reviewer = User.objects.create_user(username="gps-reviewer")
    user = User.objects.create_user(username="gps-collector")
    profile = CollectorProfile.objects.create(
        user=user, approval_status="approved", approved_by=reviewer,
        approved_at=timezone.now(), availability="offline",
    )
    return user, profile


@override_settings(**GPS_LIMITS)
class LocationPersistenceTests(TestCase):
    """Run against a disposable PostGIS test database, not operational records."""

    def setUp(self):
        self.user, self.profile = approved_collector()

    def test_manual_offline_submission_creates_geography_without_changing_availability(self):
        result = submit_collector_location(self.user.pk, observation())
        location = CollectorLocation.objects.get(collector=self.profile)
        self.assertEqual(location.position.srid, 4326)
        self.assertAlmostEqual(location.position.x, 120.9842)
        self.assertTrue(result["saved"])
        self.assertEqual(result["matching_eligibility"], "not_evaluated")
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.availability, "offline")
        self.assertNotIn("collector_id", result)

    def test_exact_retry_preserves_receipt_time(self):
        payload = observation()
        first = submit_collector_location(self.user.pk, payload)
        second = submit_collector_location(self.user.pk, payload)
        self.assertFalse(second["changed"])
        self.assertEqual(first["location"]["received_at"], second["location"]["received_at"])
        self.assertEqual(CollectorLocation.objects.count(), 1)

    def test_newer_replaces_and_older_cannot_overwrite(self):
        old = timezone.now() - timedelta(seconds=20)
        submit_collector_location(self.user.pk, observation(old))
        newer = observation(longitude=121.01)
        submit_collector_location(self.user.pk, newer)
        with self.assertRaises(LocationSubmissionError) as caught:
            submit_collector_location(self.user.pk, observation(old))
        self.assertEqual(caught.exception.code, "older_observation")
        self.assertEqual(CollectorLocation.objects.get().position.x, 121.01)
        self.assertEqual(CollectorLocation.objects.count(), 1)

    def test_same_time_different_observation_is_conflict(self):
        payload = observation()
        submit_collector_location(self.user.pk, payload)
        with self.assertRaises(LocationSubmissionError) as caught:
            submit_collector_location(self.user.pk, {**payload, "longitude": 121.01})
        self.assertEqual(caught.exception.code, "conflicting_observation")

    def test_unapproved_missing_and_inactive_collectors_cannot_save(self):
        for status in ["pending", "rejected", "suspended"]:
            self.profile.approval_status = status
            self.profile.save(update_fields=["approval_status"])
            with self.assertRaises(LocationSubmissionError) as caught:
                submit_collector_location(self.user.pk, observation())
            self.assertEqual(caught.exception.status, 403)
        self.profile.approval_status = "approved"
        self.profile.save(update_fields=["approval_status"])
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        with self.assertRaises(LocationSubmissionError):
            submit_collector_location(self.user.pk, observation())
        other = get_user_model().objects.create_user(username="gps-no-profile")
        with self.assertRaises(LocationSubmissionError):
            submit_collector_location(other.pk, observation())
        self.assertFalse(CollectorLocation.objects.exists())

    def test_authenticated_endpoint_with_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        url = reverse("assignments:submit_location")
        self.assertEqual(client.post(url, observation(), content_type="application/json").status_code, 403)
        # Login page generates the standard token used by the frontend.
        client.get(reverse("login"))
        if "csrftoken" not in client.cookies:
            # An authenticated login redirects; obtain a token from the dashboard.
            client.get(reverse("collector_dashboard"))
        token = client.cookies["csrftoken"].value
        response = client.post(url, observation(), content_type="application/json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)


@override_settings(**GPS_LIMITS)
class LocationConcurrencyTests(TransactionTestCase):
    def test_simultaneous_first_submissions_preserve_one_latest_row(self):
        if connections["default"].vendor != "postgresql":
            self.skipTest("Requires PostgreSQL row locks.")
        user, profile = approved_collector()
        now = timezone.now()
        payloads = [observation(now - timedelta(seconds=1)), observation(now, longitude=121.01)]
        barrier = Barrier(2, timeout=10)

        def submit(payload):
            close_old_connections()
            try:
                barrier.wait()
                try:
                    return submit_collector_location(user.pk, payload)
                except LocationSubmissionError as exc:
                    if exc.code != "older_observation":
                        raise
                    return {"error": exc.code}
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit, payload) for payload in payloads]
            for future in futures:
                future.result(timeout=20)
        self.assertEqual(CollectorLocation.objects.filter(collector=profile).count(), 1)
        location = CollectorLocation.objects.get(collector=profile)
        self.assertEqual(location.captured_at, now)
        self.assertEqual(location.position.x, 121.01)


from apps.accounts.models import Account, Client as CreditorClient, Customer, CustomerAddress
from apps.collection.models import CommissionRule
from apps.core.models import Notification
from apps.user.models import ClientAccess
from .models import AccountOffer, AccountAssignment
from .services import (OfferWorkflowError, _offer_payload, approximate_area,
                       change_availability, location_valid, offers_state, refresh_offers,
                       respond_to_offer, terms_display, recent_acceptances, acceptance_allowance, _eligible)
from . import views as assignment_views
from django.contrib.gis.geos import Point


class OfferPureTests(SimpleTestCase):
    def test_approximate_marker_never_returns_original_precise_pin(self):
        point = Point(120.9842, 14.5995, srid=4326)
        first = approximate_area(point)
        self.assertEqual(first, approximate_area(point))
        self.assertNotEqual(first["latitude"], point.y)
        self.assertNotEqual(first["longitude"], point.x)
        for value in [Point(180,90,srid=4326),Point(-180,-90,srid=4326)]:
            area = approximate_area(value)
            self.assertTrue(-90 <= area["latitude"] <= 90)
            self.assertTrue(-180 <= area["longitude"] <= 180)

    def test_matching_uses_client_limits_and_captured_time(self):
        now = timezone.now()
        location = SimpleNamespace(captured_at=now-timedelta(seconds=400),accuracy_m=Decimal("20"))
        client = SimpleNamespace(gps_max_age_seconds=600,gps_max_clock_skew_seconds=120,gps_max_accuracy_m=Decimal("30"))
        self.assertTrue(location_valid(location,client,now))
        client.gps_max_age_seconds=300
        self.assertFalse(location_valid(location,client,now))
        location.captured_at=now+timedelta(seconds=121)
        self.assertFalse(location_valid(location,client,now))
        self.assertFalse(location_valid(None,client,now))

    def test_percentage_fixed_and_tier_labels(self):
        terms={"earning_basis":"verified_payment","kind":"percentage","percentage":"10.0000"}
        self.assertEqual(terms_display(terms),'10% of verified payment')
        terms.update(kind="fixed",fixed_amount="250.00",currency="PHP",fixed_frequency="once_per_account")
        self.assertIn('once per account',terms_display(terms))
        terms.update(kind="tiered",tier_mode="marginal",tiers=[{"lower":"0.00","upper":None,"percentage":"12.0000"}])
        self.assertIn('12%',terms_display(terms))

    def test_offer_payload_omits_precise_coordinates_identity_and_address(self):
        offer=SimpleNamespace(pk=1,account=SimpleNamespace(reference='TEST-001'),target_address=SimpleNamespace(territory_id=None),target_position=Point(120.9842,14.5995,srid=4326),distance_m=Decimal('853.27'),matching_snapshot={"commission_terms":{"earning_basis":"verified_payment","kind":"percentage","percentage":"10"}},expires_at=timezone.now()+timedelta(minutes=10))
        data=_offer_payload(offer)
        self.assertEqual(data['distance_m'],900)
        for key in ['address','customer_name','target_position','collector_position','latitude','longitude']:
            self.assertNotIn(key,data)
        self.assertEqual(data['accept_url'],'/collector/offers/1/accept/')

    def test_mutations_require_csrf_and_authentication(self):
        factory=RequestFactory()
        for view,args in [(assignment_views.availability,()),(assignment_views.offers_refresh,()),(assignment_views.accept_offer,(1,)),(assignment_views.decline_offer,(1,))]:
            with self.subTest(view=view.__name__):
                request=factory.post('/collector/',{},content_type='application/json')
                request.user=AnonymousUser()
                self.assertEqual(view(request,*args).status_code,403)
                request._dont_enforce_csrf_checks=True
                self.assertEqual(view(request,*args).status_code,401)

    def test_list_get_is_read_only_and_routes_are_distinct(self):
        request=RequestFactory().get('/collector/offers/data/')
        request.user=SimpleNamespace(pk=7,is_authenticated=True,is_active=True)
        with patch('apps.assignments.views.offers_state',return_value={'offers':[]}) as read, patch('apps.assignments.views.refresh_offers') as write:
            self.assertEqual(assignment_views.offers_list(request).status_code,200)
            read.assert_called_once_with(7)
            write.assert_not_called()
        self.assertEqual(reverse('assignments:account_offers'),'/collector/offers/')
        self.assertEqual(reverse('assignments:my_accounts'),'/collector/accounts/')


def offer_fixture(second=False):
    user,profile=approved_collector()
    profile.availability='online';profile.capacity_limit=2
    profile.save(update_fields=['availability','capacity_limit'])
    client=CreditorClient.objects.create(code='TEST-OFFERS',name='Fictional Client',matching_strategy='fan_out',max_offer_recipients=2)
    ClientAccess.objects.create(user=user,client=client)
    location=CollectorLocation.objects.create(collector=profile,position=Point(120.9842,14.5995,srid=4326),accuracy_m=18,captured_at=timezone.now())
    customer=Customer.objects.create(client=client,source_system='TEST',display_name='Fictional Debtor')
    address=CustomerAddress.objects.create(customer=customer,kind='residential',address_line='Private fictional address',position=Point(120.9850,14.6000,srid=4326),verification_status='verified',verified_by=user,verified_at=timezone.now())
    rule=CommissionRule.objects.create(client=client,code='TEST',version=1,name='Provisional terms',kind='percentage',percentage=10,earning_basis='verified_payment')
    account=Account.objects.create(client=client,customer=customer,source_system='TEST',external_id='TEST-ACC-001',reference='TEST-ACC-001',balance_snapshot=1000,balance_as_of=timezone.now(),collection_address=address,commission_rule=rule)
    if second:
        other_user=get_user_model().objects.create_user(username='gps-other')
        other=CollectorProfile.objects.create(user=other_user,approval_status='approved',approved_by=user,approved_at=timezone.now(),availability='online',capacity_limit=2)
        ClientAccess.objects.create(user=other_user,client=client)
        CollectorLocation.objects.create(collector=other,position=Point(120.9840,14.5994,srid=4326),accuracy_m=18,captured_at=timezone.now())
    return user,profile,client,account


class OfferWorkflowDatabaseTests(TestCase):
    def setUp(self):
        self.user,self.profile,self.creditor,self.account=offer_fixture()

    def test_refresh_is_repeat_safe_and_private(self):
        first=refresh_offers(self.user.pk)
        second=refresh_offers(self.user.pk)
        self.assertEqual(len(first['offers']),1)
        self.assertEqual(first['offers'][0]['id'],second['offers'][0]['id'])
        self.assertEqual(AccountOffer.objects.count(),1)
        self.assertEqual(Notification.objects.count(),1)
        self.assertNotIn(self.account.customer.display_name,json.dumps(first))
        self.assertNotIn(self.account.collection_address.address_line,json.dumps(first))

    def test_accept_and_retry_preserve_one_owner_and_deadline(self):
        offer_id=refresh_offers(self.user.pk)['offers'][0]['id']
        first=respond_to_offer(self.user.pk,offer_id,'accept')
        second=respond_to_offer(self.user.pk,offer_id,'accept')
        self.assertEqual(first['assignment'],second['assignment'])
        self.assertEqual(AccountAssignment.objects.count(),1)
        self.assertEqual(first['workload'],1)
        self.assertIn('https://www.google.com/maps/dir/',first['assignment']['directions_url'])
        self.assertEqual(AccountOffer.objects.get().status,'accepted')

    def test_my_accounts_search_preserves_assignment_count(self):
        offer_id=refresh_offers(self.user.pk)['offers'][0]['id']
        respond_to_offer(self.user.pk,offer_id,'accept')
        self.client.force_login(self.user)
        url=reverse('assignments:my_accounts')
        for query in [self.account.reference, self.account.customer.display_name, 'fictional address']:
            response=self.client.get(url,{'q':query})
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.context['page_obj'].paginator.count,1)
            self.assertContains(response,self.account.reference)
        response=self.client.get(url,{'q':'no matching customer','page':'not-a-number'})
        self.assertEqual(response.context['assigned_count'],1)
        self.assertEqual(response.context['page_obj'].paginator.count,0)
        self.assertContains(response,'No matching accounts')
        self.assertNotContains(response,self.account.customer.display_name)

    def test_my_accounts_hides_other_collectors_and_revoked_client_access(self):
        offer_id=refresh_offers(self.user.pk)['offers'][0]['id']
        result=respond_to_offer(self.user.pk,offer_id,'accept')
        other=get_user_model().objects.create_user(username='work-list-other')
        CollectorProfile.objects.create(user=other,approval_status='approved',approved_by=self.user,approved_at=timezone.now())
        ClientAccess.objects.create(user=other,client=self.creditor)
        self.client.force_login(other)
        response=self.client.get(reverse('assignments:my_accounts'),{'q':self.account.reference})
        self.assertEqual(response.context['assigned_count'],0)
        self.assertNotContains(response,self.account.customer.display_name)
        self.client.force_login(self.user)
        ClientAccess.objects.filter(user=self.user,client=self.creditor).update(is_active=False)
        response=self.client.get(reverse('assignments:my_accounts'))
        self.assertEqual(response.context['assigned_count'],0)
        self.assertNotContains(response,self.account.reference)
        self.assertEqual(self.client.get(result['assignment']['detail_url']).status_code,404)

    def test_offline_missing_scope_stale_and_capacity_states(self):
        change_availability(self.user.pk,'offline')
        self.assertEqual(refresh_offers(self.user.pk)['state'],'offline')
        change_availability(self.user.pk,'online')
        ClientAccess.objects.filter(user=self.user).update(is_active=False)
        self.assertEqual(offers_state(self.user.pk)['state'],'no_client_access')
        ClientAccess.objects.filter(user=self.user).update(is_active=True)
        CollectorLocation.objects.filter(collector=self.profile).update(captured_at=timezone.now()-timedelta(seconds=301))
        self.assertEqual(offers_state(self.user.pk)['state'],'location_required')

    def test_decline_does_not_immediately_reoffer_same_invitation(self):
        offer_id=refresh_offers(self.user.pk)['offers'][0]['id']
        self.assertTrue(respond_to_offer(self.user.pk,offer_id,'decline')['declined'])
        self.assertEqual(refresh_offers(self.user.pk)['offers'],[])

    def test_expired_offer_is_not_accepted_even_without_worker(self):
        offer_id=refresh_offers(self.user.pk)['offers'][0]['id']
        with patch('apps.assignments.services.timezone.now',return_value=timezone.now()+timedelta(minutes=11)):
            with self.assertRaises(OfferWorkflowError) as caught:
                respond_to_offer(self.user.pk,offer_id,'accept')
        self.assertEqual(caught.exception.code,'offer_unavailable')
        self.assertFalse(AccountAssignment.objects.exists())

    def test_changed_earning_terms_block_acceptance(self):
        offer_id=refresh_offers(self.user.pk)['offers'][0]['id']
        CommissionRule.objects.filter(pk=self.account.commission_rule_id).update(percentage=12)
        with self.assertRaises(OfferWorkflowError) as caught:
            respond_to_offer(self.user.pk,offer_id,'accept')
        self.assertEqual(caught.exception.code,'terms_changed')

    def test_out_of_radius_and_missing_coordinates_are_not_offered(self):
        CustomerAddress.objects.filter(pk=self.account.collection_address_id).update(position=Point(121.2,14.8,srid=4326))
        self.assertEqual(refresh_offers(self.user.pk)['offers'],[])
        CustomerAddress.objects.filter(pk=self.account.collection_address_id).update(position=None)
        self.assertEqual(refresh_offers(self.user.pk)['offers'],[])

    def test_other_collectors_cannot_claim_or_read_details(self):
        offer_id=refresh_offers(self.user.pk)['offers'][0]['id']
        other=get_user_model().objects.create_user(username='unrelated-collector')
        CollectorProfile.objects.create(user=other,approval_status='approved',approved_by=self.user,approved_at=timezone.now())
        with self.assertRaises(OfferWorkflowError) as caught:
            respond_to_offer(other.pk,offer_id,'accept')
        self.assertEqual(caught.exception.status,404)
        assignment=respond_to_offer(self.user.pk,offer_id,'accept')['assignment']
        self.client.force_login(other)
        self.assertEqual(self.client.get(assignment['detail_url']).status_code,404)

    def test_legacy_capacity_does_not_block_accepting_another_account(self):
        self.profile.capacity_limit=1;self.profile.save(update_fields=['capacity_limit'])
        offer_id=refresh_offers(self.user.pk)['offers'][0]['id']
        result=respond_to_offer(self.user.pk,offer_id,'accept')
        self.assertEqual(result['daily_acceptance']['used'],1)
        self.assertEqual(refresh_offers(self.user.pk)['state'],'ready')
        second=Account.objects.create(client=self.creditor,customer=self.account.customer,source_system='TEST',external_id='SECOND-ALLOWANCE',reference='SECOND-ALLOWANCE',balance_snapshot=1000,balance_as_of=timezone.now(),collection_address=self.account.collection_address,commission_rule=self.account.commission_rule)
        offers=refresh_offers(self.user.pk)['offers']
        self.assertEqual(len(offers),1)
        result=respond_to_offer(self.user.pk,offers[0]['id'],'accept')
        self.assertEqual(result['daily_acceptance']['used'],2)
        self.assertEqual(AccountAssignment.objects.filter(collector=self.profile,status='active').count(),2)
        change_availability(self.user.pk,'offline')
        self.assertEqual(AccountAssignment.objects.filter(status='active').count(),2)

    def test_nearest_only_invites_closest_eligible_collector(self):
        other=get_user_model().objects.create_user(username='closer-collector')
        profile=CollectorProfile.objects.create(user=other,approval_status='approved',approved_by=self.user,approved_at=timezone.now(),availability='online')
        ClientAccess.objects.create(user=other,client=self.creditor)
        CollectorLocation.objects.create(collector=profile,position=self.account.collection_address.position,accuracy_m=5,captured_at=timezone.now())
        self.creditor.matching_strategy='nearest';self.creditor.save(update_fields=['matching_strategy'])
        self.assertEqual(refresh_offers(self.user.pk)['offers'],[])
        self.assertEqual(AccountOffer.objects.get().collector_id,profile.pk)


class OfferAcceptanceConcurrencyTests(TransactionTestCase):
    def test_two_collectors_can_never_create_two_active_owners(self):
        user,profile,client,account=offer_fixture(second=True)
        refresh_offers(user.pk)
        offers=list(AccountOffer.objects.values_list('collector__user_id','pk'))
        self.assertEqual(len(offers),2)
        barrier=Barrier(2,timeout=10)
        def accept(pair):
            close_old_connections()
            try:
                barrier.wait()
                try:
                    return respond_to_offer(pair[0],pair[1],'accept')
                except OfferWorkflowError as exc:
                    return {'error':exc.code}
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(accept,pair) for pair in offers]
            results=[future.result(timeout=20) for future in futures]
        self.assertEqual(sum(result.get('accepted') is True for result in results),1)
        self.assertEqual(AccountAssignment.objects.filter(account=account,status='active').count(),1)


class RollingAllowancePureTests(SimpleTestCase):
    def test_window_is_global_status_independent_and_excludes_manual_assignments(self):
        now=timezone.now()
        with patch('apps.assignments.models.AccountAssignment.objects') as records:
            profile=SimpleNamespace(account_assignments=records)
            recent_acceptances(profile,now)
            records.filter.assert_called_once_with(offer__isnull=False,created_at__gt=now-timedelta(hours=24),created_at__lte=now)

    def test_remaining_slots_and_next_time_include_historical_excess(self):
        now=timezone.now();oldest=now-timedelta(hours=3)
        with patch('apps.assignments.services.recent_acceptances') as history:
            history.return_value.count.return_value=9
            quota=acceptance_allowance(SimpleNamespace(),now)
            self.assertEqual(quota,{'used':9,'limit':10,'remaining':1,'next_available_at':None})
            history.return_value.count.return_value=10
            history.return_value.order_by.return_value.values_list.return_value.__getitem__.return_value=oldest
            quota=acceptance_allowance(SimpleNamespace(),now)
            self.assertEqual(quota['remaining'],0)
            self.assertEqual(quota['next_available_at'],(oldest+timedelta(hours=24)).isoformat())
            history.return_value.count.return_value=12
            acceptance_allowance(SimpleNamespace(),now)
            history.return_value.order_by.return_value.values_list.return_value.__getitem__.assert_called_with(2)

    def test_eligibility_uses_daily_count_instead_of_active_capacity(self):
        now=timezone.now()
        user=SimpleNamespace(is_authenticated=True,is_active=True,is_superuser=False,has_perm=lambda name:False)
        profile=SimpleNamespace(pk=1,user_id=1,user=user,approval_status='approved',availability='online',capacity_limit=1)
        client=SimpleNamespace(is_active=True,require_service_area_membership=False,gps_max_age_seconds=300,gps_max_clock_skew_seconds=120,gps_max_accuracy_m=50)
        location=SimpleNamespace(captured_at=now,accuracy_m=5)
        with patch('apps.assignments.services.recent_acceptances') as history,patch('apps.assignments.services.ClientAccess.objects') as grants,patch('apps.assignments.services.active_workload') as workload:
            grants.filter.return_value.exists.return_value=True
            history.return_value.count.return_value=9
            _eligible(profile,client,location,now)
            workload.assert_not_called()
            history.return_value.count.return_value=10
            with self.assertRaises(OfferWorkflowError) as caught:_eligible(profile,client,location,now)
            self.assertEqual(caught.exception.code,'daily_limit_reached')


def quota_account(source):
    token=uuid4().hex
    return Account.objects.create(client=source.client,customer=source.customer,source_system='TEST',external_id=token,reference=token,balance_snapshot=1000,balance_as_of=timezone.now(),collection_address=source.collection_address,commission_rule=source.commission_rule)


def quota_history(profile,source,stamp,status='released',offer=True):
    account=quota_account(source)
    if status!='active':
        Account.objects.filter(pk=account.pk).update(status='on_hold')
    invitation=None
    if offer:
        location=CollectorLocation.objects.get(collector=profile)
        invitation=AccountOffer.objects.create(account=account,collector=profile,target_address=account.collection_address,
            target_position=account.collection_address.position,commission_rule=account.commission_rule,status='accepted',
            expires_at=timezone.now()+timedelta(hours=1),responded_at=stamp,collector_position=location.position,
            location_captured_at=location.captured_at,distance_m=0,matching_snapshot={})
    assignment=AccountAssignment.objects.create(account=account,collector=profile,offer=invitation,target_address=account.collection_address,
        target_position=account.collection_address.position,commission_rule=account.commission_rule,status=status,
        first_action_due_at=timezone.now()+timedelta(hours=24),ended_at=timezone.now() if status!='active' else None,policy_snapshot={})
    AccountAssignment.objects.filter(pk=assignment.pk).update(created_at=stamp)
    return assignment


class RollingAllowanceDatabaseTests(TestCase):
    setUp=OfferWorkflowDatabaseTests.setUp

    def test_exact_24_hour_boundary_release_and_manual_assignment_rules(self):
        now=timezone.now()
        quota_history(self.profile,self.account,now-timedelta(hours=24))
        quota_history(self.profile,self.account,now-timedelta(hours=24)+timedelta(microseconds=1))
        quota_history(self.profile,self.account,now,status='completed')
        quota_history(self.profile,self.account,now,offer=False,status='active')
        self.assertEqual(acceptance_allowance(self.profile,now)['used'],2)
        self.assertEqual(acceptance_allowance(self.profile,now+timedelta(microseconds=1))['used'],1)
        self.assertEqual(AccountAssignment.objects.filter(collector=self.profile,status='active').count(),1)

    def test_tenth_acceptance_retry_and_released_history_still_blocks_eleventh(self):
        now=timezone.now()
        for _ in range(9):quota_history(self.profile,self.account,now-timedelta(hours=1))
        second=quota_account(self.account)
        offers=refresh_offers(self.user.pk)['offers']
        self.assertEqual(len(offers),2)
        tenth=respond_to_offer(self.user.pk,offers[0]['id'],'accept')
        self.assertEqual(tenth['daily_acceptance']['used'],10)
        retry=respond_to_offer(self.user.pk,offers[0]['id'],'accept')
        self.assertEqual(retry['daily_acceptance']['used'],10)
        with self.assertRaises(OfferWorkflowError) as caught:respond_to_offer(self.user.pk,offers[1]['id'],'accept')
        self.assertEqual(caught.exception.code,'daily_limit_reached')
        self.assertEqual(offers_state(self.user.pk)['state'],'daily_limit_reached')
        AccountAssignment.objects.filter(offer_id=offers[0]['id']).update(status='released',ended_at=timezone.now())
        self.assertEqual(acceptance_allowance(self.profile)['used'],10)
        self.assertEqual(offers_state(self.user.pk)['offers'],[])

    def test_old_owned_accounts_do_not_block_new_offers_and_pages_show_daily_quota(self):
        old=timezone.now()-timedelta(hours=25)
        for _ in range(12):quota_history(self.profile,self.account,old,status='active')
        self.assertEqual(offers_state(self.user.pk)['state'],'ready')
        self.assertEqual(len(refresh_offers(self.user.pk)['offers']),1)
        self.client.force_login(self.user)
        response=self.client.get(reverse('assignments:my_accounts'))
        self.assertContains(response,'Accepted in last 24 hours: 0/10')
        self.assertNotContains(response,'Capacity:')
        response=self.client.get(reverse('collector_dashboard'))
        self.assertContains(response,'Active accounts: 12')
        self.assertNotContains(response,'Capacity Limit')


class RollingAllowanceConcurrencyTests(TransactionTestCase):
    def test_two_accounts_compete_for_one_remaining_slot(self):
        user,profile,client,account=offer_fixture()
        for _ in range(9):quota_history(profile,account,timezone.now()-timedelta(hours=1))
        quota_account(account)
        offers=refresh_offers(user.pk)['offers']
        self.assertEqual(len(offers),2)
        barrier=Barrier(2,timeout=10)
        def accept(offer_id):
            close_old_connections()
            try:
                barrier.wait()
                return respond_to_offer(user.pk,offer_id,'accept')
            except OfferWorkflowError as exc:return {'error':exc.code}
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(accept,item['id']) for item in offers]
            results=[future.result(timeout=20) for future in futures]
        self.assertEqual(sum(r.get('accepted') is True for r in results),1)
        self.assertEqual(sum(r.get('error')=='daily_limit_reached' for r in results),1)
        self.assertEqual(acceptance_allowance(profile)['used'],10)


class ReleaseRequestModelValidationTests(SimpleTestCase):
    def test_reason_validation(self):
        from .models import AssignmentReleaseRequest, AccountAssignment
        from apps.accounts.models import Account
        from apps.user.models import User
        import uuid

        user = User(pk=1)
        acc = Account(pk=10)
        assign = AccountAssignment(pk=20, account=acc)

        req = AssignmentReleaseRequest(
            assignment=assign, requested_by=user, event_id=uuid.uuid4(),
            reason="   ", status="requested"
        )
        with self.assertRaises(ValidationError):
            req.clean()

        long_reason = "x" * 5001
        req.reason = long_reason
        with self.assertRaises(ValidationError):
            req.clean()

    def test_rejected_status_requires_review_reason(self):
        from .models import AssignmentReleaseRequest, AccountAssignment
        from apps.accounts.models import Account
        from apps.user.models import User
        import uuid

        user = User(pk=1)
        reviewer = User(pk=2)
        acc = Account(pk=10)
        assign = AccountAssignment(pk=20, account=acc)

        req = AssignmentReleaseRequest(
            assignment=assign, requested_by=user, event_id=uuid.uuid4(),
            reason="Relocating to another city.", status="rejected",
            reviewed_by=reviewer, reviewed_at=timezone.now(), review_reason="   "
        )
        with self.assertRaises(ValidationError):
            req.clean()

        req.review_reason = "Please finish existing visit first."
        req.clean()

    def test_pending_versus_finalized_consistency(self):
        from .models import AssignmentReleaseRequest, AccountAssignment
        from apps.accounts.models import Account
        from apps.user.models import User
        import uuid

        user = User(pk=1)
        reviewer = User(pk=2)
        acc = Account(pk=10)
        assign = AccountAssignment(pk=20, account=acc)

        # Pending with reviewer details is invalid
        req_bad_pending = AssignmentReleaseRequest(
            assignment=assign, requested_by=user, event_id=uuid.uuid4(),
            reason="Need release.", status="requested",
            reviewed_by=reviewer, reviewed_at=timezone.now()
        )
        with self.assertRaises(ValidationError):
            req_bad_pending.clean()

        # Approved without reviewer details is invalid
        req_bad_approved = AssignmentReleaseRequest(
            assignment=assign, requested_by=user, event_id=uuid.uuid4(),
            reason="Need release.", status="approved",
            reviewed_by=None, reviewed_at=None
        )
        with self.assertRaises(ValidationError):
            req_bad_approved.clean()

    def test_has_ongoing_visit_property(self):
        from .models import AssignmentReleaseRequest, AccountAssignment
        from apps.activities.models import FieldActivity
        from apps.accounts.models import Account
        from apps.user.models import User
        from unittest.mock import MagicMock
        import uuid

        user = User(pk=1)
        acc = Account(pk=10)
        assign = AccountAssignment(pk=20, account=acc)
        req = AssignmentReleaseRequest(
            assignment=assign, requested_by=user, event_id=uuid.uuid4(),
            reason="Need release.", status="requested"
        )

        # Mock prefetched activities with one in_progress
        act_progress = FieldActivity(pk=1, status="in_progress")
        act_done = FieldActivity(pk=2, status="submitted")

        assign._prefetched_objects_cache = {"activities": [act_done, act_progress]}
        self.assertTrue(req.has_ongoing_visit)

        assign._prefetched_objects_cache = {"activities": [act_done]}
        self.assertFalse(req.has_ongoing_visit)


class ReleaseRequestPayloadValidationTests(SimpleTestCase):
    def test_valid_payload(self):
        from .services import validate_release_request_payload
        import uuid
        test_uuid = str(uuid.uuid4())
        data = validate_release_request_payload({
            "event_id": test_uuid,
            "reason": "  Collector has personal emergency.  ",
        })
        self.assertEqual(data["event_id"], test_uuid)
        self.assertEqual(data["reason"], "Collector has personal emergency.")

    def test_invalid_uuid(self):
        from .services import validate_release_request_payload, OfferWorkflowError
        for bad_id in ["not-a-uuid", 12345, None, ""]:
            with self.subTest(bad_id=bad_id), self.assertRaises(OfferWorkflowError):
                validate_release_request_payload({"event_id": bad_id, "reason": "Reasonable note."})

    def test_empty_or_oversized_reason(self):
        from .services import validate_release_request_payload, OfferWorkflowError
        import uuid
        test_uuid = str(uuid.uuid4())
        with self.assertRaises(OfferWorkflowError):
            validate_release_request_payload({"event_id": test_uuid, "reason": "   "})
        with self.assertRaises(OfferWorkflowError):
            validate_release_request_payload({"event_id": test_uuid, "reason": "x" * 5001})


class ReleaseRequestDecisionValidationTests(SimpleTestCase):
    def test_decision_validation(self):
        from .services import validate_release_decision, OfferWorkflowError

        self.assertEqual(validate_release_decision("approve", "Approved by team lead"), "Approved by team lead")
        self.assertEqual(validate_release_decision("approve", ""), "")

        # Rejection requires reason
        with self.assertRaises(OfferWorkflowError):
            validate_release_decision("reject", "   ")

        self.assertEqual(validate_release_decision("reject", "Active promise pending"), "Active promise pending")

        # Invalid decision choice
        with self.assertRaises(OfferWorkflowError):
            validate_release_decision("cancel", "some reason")


class ReleaseRequestPermissionTests(SimpleTestCase):
    def test_can_review_releases(self):
        from .services import can_review_releases
        from apps.user.models import User
        from unittest.mock import MagicMock

        # Inactive or None
        self.assertFalse(can_review_releases(None))
        self.assertFalse(can_review_releases(User(pk=1, is_active=False)))

        # Superuser
        super_user = User(pk=2, is_active=True, is_superuser=True)
        self.assertTrue(can_review_releases(super_user))

        # Permission check
        perm_user = User(pk=3, is_active=True)
        perm_user.has_perm = MagicMock(side_effect=lambda p: p == "assignments.review_release_requests")
        self.assertTrue(can_review_releases(perm_user))

        no_perm_user = User(pk=4, is_active=True)
        no_perm_user.has_perm = MagicMock(return_value=False)
        self.assertFalse(can_review_releases(no_perm_user))

    def test_can_review_releases_clears_cached_permissions(self):
        from .services import can_review_releases
        from apps.user.models import User
        from unittest.mock import MagicMock

        user = User(pk=5, is_active=True)
        user._perm_cache = {"assignments.review_release_requests"}
        user._user_perm_cache = {"assignments.review_release_requests"}
        user.has_perm = MagicMock(return_value=False)

        self.assertFalse(can_review_releases(user))
        self.assertFalse(hasattr(user, "_perm_cache"))
        self.assertFalse(hasattr(user, "_user_perm_cache"))


from contextlib import contextmanager

@contextmanager
def _mock_atomic():
    yield


class ReleaseRequestWorkflowPureTests(SimpleTestCase):
    def test_client_flag_prohibits_request(self):
        from unittest.mock import MagicMock, patch
        from .services import request_assignment_release, OfferWorkflowError
        import uuid

        with patch("django.contrib.auth.get_user_model") as mock_user_cls, \
             patch("apps.assignments.services.has_backoffice_access", return_value=False), \
             patch("apps.assignments.services.collector_profile") as mock_prof, \
             patch("apps.assignments.services.AccountAssignment.objects") as mock_assign_mgr, \
             patch("apps.assignments.services.Account.objects") as mock_acc_mgr, \
             patch("apps.assignments.services.CollectorProfile.objects") as mock_prof_mgr, \
             patch("apps.assignments.services.Client.objects") as mock_client_mgr, \
             patch("apps.assignments.services.ClientAccess.objects") as mock_grant_mgr, \
             patch("apps.assignments.services.AssignmentReleaseRequest.objects") as mock_req_mgr, \
             patch("apps.assignments.services.transaction.atomic", side_effect=_mock_atomic):

            user = MagicMock(pk=1, is_active=True)
            mock_user_cls.return_value.objects.filter.return_value.first.return_value = user

            profile = MagicMock(pk=5, approval_status="approved", user=user)
            mock_prof.return_value = profile

            mock_assign_mgr.filter.return_value.values.return_value.first.return_value = {
                "account_id": 20, "collector_id": 5
            }

            account = MagicMock(pk=20, client_id=10)
            mock_acc_mgr.select_for_update.return_value.get.return_value = account
            mock_prof_mgr.select_for_update.return_value.select_related.return_value.get.return_value = profile

            assignment = MagicMock(pk=30, collector_id=5, status="active")
            mock_assign_mgr.select_for_update.return_value.get.return_value = assignment

            # Client has allow_collector_release = False
            client = MagicMock(pk=10, is_active=True, allow_collector_release=False)
            mock_client_mgr.select_for_update.return_value.get.return_value = client

            grant = MagicMock(is_active=True)
            mock_grant_mgr.select_for_update.return_value.filter.return_value.first.return_value = grant

            mock_req_mgr.select_for_update.return_value.filter.return_value.first.return_value = None

            payload = {"event_id": str(uuid.uuid4()), "reason": "Want release"}
            with self.assertRaises(OfferWorkflowError) as cm:
                request_assignment_release(1, 30, payload)
            self.assertEqual(cm.exception.code, "release_not_allowed")

    def test_uuid_idempotency_retry_and_conflict(self):
        from unittest.mock import MagicMock, patch
        from .services import request_assignment_release, OfferWorkflowError
        import uuid

        event_id = str(uuid.uuid4())
        existing_req = MagicMock(assignment_id=30, requested_by_id=1, reason="Relocating")

        with patch("django.contrib.auth.get_user_model") as mock_user_cls, \
             patch("apps.assignments.services.has_backoffice_access", return_value=False), \
             patch("apps.assignments.services.collector_profile") as mock_prof, \
             patch("apps.assignments.services.AccountAssignment.objects") as mock_assign_mgr, \
             patch("apps.assignments.services.Account.objects") as mock_acc_mgr, \
             patch("apps.assignments.services.CollectorProfile.objects") as mock_prof_mgr, \
             patch("apps.assignments.services.Client.objects") as mock_client_mgr, \
             patch("apps.assignments.services.ClientAccess.objects") as mock_grant_mgr, \
             patch("apps.assignments.services.AssignmentReleaseRequest.objects") as mock_req_mgr, \
             patch("apps.assignments.services.transaction.atomic", side_effect=_mock_atomic):

            user = MagicMock(pk=1, is_active=True)
            mock_user_cls.return_value.objects.filter.return_value.first.return_value = user
            profile = MagicMock(pk=5, approval_status="approved", user=user)
            mock_prof.return_value = profile

            mock_assign_mgr.filter.return_value.values.return_value.first.return_value = {
                "account_id": 20, "collector_id": 5
            }
            mock_acc_mgr.select_for_update.return_value.get.return_value = MagicMock(pk=20, client_id=10)
            mock_prof_mgr.select_for_update.return_value.select_related.return_value.get.return_value = profile
            mock_assign_mgr.select_for_update.return_value.get.return_value = MagicMock(pk=30, collector_id=5, status="active")
            mock_client_mgr.select_for_update.return_value.get.return_value = MagicMock(pk=10, is_active=True, allow_collector_release=True)
            mock_grant_mgr.select_for_update.return_value.filter.return_value.first.return_value = MagicMock(is_active=True)

            # Idempotent retry with identical data
            mock_req_mgr.select_for_update.return_value.filter.return_value.first.return_value = existing_req
            result = request_assignment_release(1, 30, {"event_id": event_id, "reason": "Relocating"})
            self.assertTrue(result["saved"])
            self.assertFalse(result["created"])
            self.assertEqual(result["request"], existing_req)

            # Conflict retry with different reason
            with self.assertRaises(OfferWorkflowError) as cm:
                request_assignment_release(1, 30, {"event_id": event_id, "reason": "Different reason"})
            self.assertEqual(cm.exception.code, "event_conflict")

    def test_retry_after_approval_acknowledges_without_restoring_access(self):
        from unittest.mock import MagicMock, patch
        from .services import request_assignment_release
        import uuid

        event_id = str(uuid.uuid4())
        # Request is already approved, assignment is released
        existing_req = MagicMock(assignment_id=30, requested_by_id=1, reason="Relocating", status="approved")

        with patch("django.contrib.auth.get_user_model") as mock_user_cls, \
             patch("apps.assignments.services.has_backoffice_access", return_value=False), \
             patch("apps.assignments.services.collector_profile") as mock_prof, \
             patch("apps.assignments.services.AccountAssignment.objects") as mock_assign_mgr, \
             patch("apps.assignments.services.Account.objects") as mock_acc_mgr, \
             patch("apps.assignments.services.CollectorProfile.objects") as mock_prof_mgr, \
             patch("apps.assignments.services.Client.objects") as mock_client_mgr, \
             patch("apps.assignments.services.ClientAccess.objects") as mock_grant_mgr, \
             patch("apps.assignments.services.AssignmentReleaseRequest.objects") as mock_req_mgr, \
             patch("apps.assignments.services.transaction.atomic", side_effect=_mock_atomic):

            user = MagicMock(pk=1, is_active=True)
            mock_user_cls.return_value.objects.filter.return_value.first.return_value = user
            profile = MagicMock(pk=5, approval_status="approved", user=user)
            mock_prof.return_value = profile

            mock_assign_mgr.filter.return_value.values.return_value.first.return_value = {
                "account_id": 20, "collector_id": 5
            }
            mock_acc_mgr.select_for_update.return_value.get.return_value = MagicMock(pk=20, client_id=10)
            mock_prof_mgr.select_for_update.return_value.select_related.return_value.get.return_value = profile
            # Assignment status is released!
            released_assignment = MagicMock(pk=30, collector_id=5, status="released")
            mock_assign_mgr.select_for_update.return_value.get.return_value = released_assignment
            mock_client_mgr.select_for_update.return_value.get.return_value = MagicMock(pk=10, is_active=True, allow_collector_release=True)
            mock_grant_mgr.select_for_update.return_value.filter.return_value.first.return_value = MagicMock(is_active=True)

            mock_req_mgr.select_for_update.return_value.filter.return_value.first.return_value = existing_req

            result = request_assignment_release(1, 30, {"event_id": event_id, "reason": "Relocating"})
            self.assertTrue(result["saved"])
            self.assertFalse(result["created"])
            self.assertEqual(result["request"], existing_req)
            self.assertEqual(result["request"].status, "approved")
            # Assignment remains released (ownership not restored)
            self.assertEqual(released_assignment.status, "released")
            released_assignment.save.assert_not_called()

    def test_revoked_collector_client_grant_denied_under_lock(self):
        from unittest.mock import MagicMock, patch
        from .services import request_assignment_release, OfferWorkflowError
        import uuid

        with patch("django.contrib.auth.get_user_model") as mock_user_cls, \
             patch("apps.assignments.services.has_backoffice_access", return_value=False), \
             patch("apps.assignments.services.collector_profile") as mock_prof, \
             patch("apps.assignments.services.AccountAssignment.objects") as mock_assign_mgr, \
             patch("apps.assignments.services.Account.objects") as mock_acc_mgr, \
             patch("apps.assignments.services.CollectorProfile.objects") as mock_prof_mgr, \
             patch("apps.assignments.services.Client.objects") as mock_client_mgr, \
             patch("apps.assignments.services.ClientAccess.objects") as mock_grant_mgr, \
             patch("apps.assignments.services.transaction.atomic", side_effect=_mock_atomic):

            user = MagicMock(pk=1, is_active=True)
            mock_user_cls.return_value.objects.filter.return_value.first.return_value = user
            profile = MagicMock(pk=5, approval_status="approved", user=user)
            mock_prof.return_value = profile

            mock_assign_mgr.filter.return_value.values.return_value.first.return_value = {
                "account_id": 20, "collector_id": 5
            }
            mock_acc_mgr.select_for_update.return_value.get.return_value = MagicMock(pk=20, client_id=10)
            mock_prof_mgr.select_for_update.return_value.select_related.return_value.get.return_value = profile
            mock_assign_mgr.select_for_update.return_value.get.return_value = MagicMock(pk=30, collector_id=5, status="active")
            mock_client_mgr.select_for_update.return_value.get.return_value = MagicMock(pk=10, is_active=True, allow_collector_release=True)

            # Grant was revoked under lock
            mock_grant_mgr.select_for_update.return_value.filter.return_value.first.return_value = MagicMock(is_active=False)

            with self.assertRaises(OfferWorkflowError) as cm:
                request_assignment_release(1, 30, {"event_id": str(uuid.uuid4()), "reason": "Moving"})
            self.assertEqual(cm.exception.code, "client_access_required")

    def test_unapproved_collector_profile_denied_under_lock(self):
        from unittest.mock import MagicMock, patch
        from .services import request_assignment_release, OfferWorkflowError
        import uuid

        with patch("django.contrib.auth.get_user_model") as mock_user_cls, \
             patch("apps.assignments.services.has_backoffice_access", return_value=False), \
             patch("apps.assignments.services.collector_profile") as mock_prof, \
             patch("apps.assignments.services.AccountAssignment.objects") as mock_assign_mgr, \
             patch("apps.assignments.services.Account.objects") as mock_acc_mgr, \
             patch("apps.assignments.services.CollectorProfile.objects") as mock_prof_mgr, \
             patch("apps.assignments.services.Client.objects") as mock_client_mgr, \
             patch("apps.assignments.services.ClientAccess.objects") as mock_grant_mgr, \
             patch("apps.assignments.services.transaction.atomic", side_effect=_mock_atomic):

            user = MagicMock(pk=1, is_active=True)
            mock_user_cls.return_value.objects.filter.return_value.first.return_value = user
            profile = MagicMock(pk=5, approval_status="approved", user=user)
            mock_prof.return_value = profile

            mock_assign_mgr.filter.return_value.values.return_value.first.return_value = {
                "account_id": 20, "collector_id": 5
            }
            mock_acc_mgr.select_for_update.return_value.get.return_value = MagicMock(pk=20, client_id=10)
            # Profile approval status changed to pending under lock
            locked_profile = MagicMock(pk=5, approval_status="pending", user=user)
            mock_prof_mgr.select_for_update.return_value.select_related.return_value.get.return_value = locked_profile
            mock_assign_mgr.select_for_update.return_value.get.return_value = MagicMock(pk=30, collector_id=5, status="active")
            mock_client_mgr.select_for_update.return_value.get.return_value = MagicMock(pk=10, is_active=True, allow_collector_release=True)
            mock_grant_mgr.select_for_update.return_value.filter.return_value.first.return_value = MagicMock(is_active=True)

            with self.assertRaises(OfferWorkflowError) as cm:
                request_assignment_release(1, 30, {"event_id": str(uuid.uuid4()), "reason": "Moving"})
            self.assertEqual(cm.exception.code, "collector_approval_required")

    def test_duplicate_pending_request_prohibited(self):
        from unittest.mock import MagicMock, patch
        from .services import request_assignment_release, OfferWorkflowError
        import uuid

        with patch("django.contrib.auth.get_user_model") as mock_user_cls, \
             patch("apps.assignments.services.has_backoffice_access", return_value=False), \
             patch("apps.assignments.services.collector_profile") as mock_prof, \
             patch("apps.assignments.services.AccountAssignment.objects") as mock_assign_mgr, \
             patch("apps.assignments.services.Account.objects") as mock_acc_mgr, \
             patch("apps.assignments.services.CollectorProfile.objects") as mock_prof_mgr, \
             patch("apps.assignments.services.Client.objects") as mock_client_mgr, \
             patch("apps.assignments.services.ClientAccess.objects") as mock_grant_mgr, \
             patch("apps.assignments.services.AssignmentReleaseRequest.objects") as mock_req_mgr, \
             patch("apps.assignments.services.transaction.atomic", side_effect=_mock_atomic):

            user = MagicMock(pk=1, is_active=True)
            mock_user_cls.return_value.objects.filter.return_value.first.return_value = user
            profile = MagicMock(pk=5, approval_status="approved", user=user)
            mock_prof.return_value = profile

            mock_assign_mgr.filter.return_value.values.return_value.first.return_value = {
                "account_id": 20, "collector_id": 5
            }
            mock_acc_mgr.select_for_update.return_value.get.return_value = MagicMock(pk=20, client_id=10)
            mock_prof_mgr.select_for_update.return_value.select_related.return_value.get.return_value = profile

            assignment = MagicMock(pk=30, collector_id=5, status="active")
            assignment.release_requests.filter.return_value.exists.return_value = True
            mock_assign_mgr.select_for_update.return_value.get.return_value = assignment

            mock_client_mgr.select_for_update.return_value.get.return_value = MagicMock(pk=10, is_active=True, allow_collector_release=True)
            mock_grant_mgr.select_for_update.return_value.filter.return_value.first.return_value = MagicMock(is_active=True)

            # Distinct event_id (no existing by event_id)
            mock_req_mgr.select_for_update.return_value.filter.return_value.first.return_value = None

            with self.assertRaises(OfferWorkflowError) as cm:
                request_assignment_release(1, 30, {"event_id": str(uuid.uuid4()), "reason": "Another reason"})
            self.assertEqual(cm.exception.code, "pending_request_exists")

    def test_self_review_prohibited(self):
        from unittest.mock import MagicMock, patch
        from .services import decide_release_request, OfferWorkflowError

        with patch("django.contrib.auth.get_user_model") as mock_user_cls, \
             patch("apps.assignments.services.can_review_releases", return_value=True), \
             patch("apps.assignments.services.AssignmentReleaseRequest.objects") as mock_req_mgr:

            user = MagicMock(pk=1, is_active=True)
            mock_user_cls.return_value.objects.filter.return_value.first.return_value = user

            # Request requested by user 1
            mock_req_mgr.filter.return_value.values.return_value.first.return_value = {
                "assignment_id": 10,
                "assignment__account_id": 20,
                "assignment__collector_id": 5,
                "requested_by_id": 1,
            }

            with self.assertRaises(OfferWorkflowError) as cm:
                decide_release_request(1, 99, "approve", "Looks good")
            self.assertEqual(cm.exception.code, "self_review")

    def test_revoked_reviewer_client_grant_denied_under_lock(self):
        from unittest.mock import MagicMock, patch
        from .services import decide_release_request, OfferWorkflowError

        with patch("django.contrib.auth.get_user_model") as mock_user_cls, \
             patch("apps.assignments.services.can_review_releases", return_value=True), \
             patch("apps.assignments.services.AssignmentReleaseRequest.objects") as mock_req_mgr, \
             patch("apps.assignments.services.Account.objects") as mock_acc_mgr, \
             patch("apps.assignments.services.CollectorProfile.objects") as mock_prof_mgr, \
             patch("apps.assignments.services.AccountAssignment.objects") as mock_assign_mgr, \
             patch("apps.assignments.services.Client.objects") as mock_client_mgr, \
             patch("apps.assignments.services.ClientAccess.objects") as mock_access_mgr, \
             patch("apps.assignments.services.transaction.atomic", side_effect=_mock_atomic):

            reviewer = MagicMock(pk=2, is_active=True)
            mock_user_cls.return_value.objects.filter.return_value.first.return_value = reviewer

            mock_req_mgr.filter.return_value.values.return_value.first.return_value = {
                "assignment_id": 10,
                "assignment__account_id": 20,
                "assignment__collector_id": 5,
                "requested_by_id": 1,
            }

            mock_acc_mgr.select_for_update.return_value.get.return_value = MagicMock(pk=20, client_id=10)
            mock_prof_mgr.select_for_update.return_value.select_related.return_value.get.return_value = MagicMock(pk=5)
            mock_assign_mgr.select_for_update.return_value.get.return_value = MagicMock(pk=10, status="active")
            mock_client_mgr.select_for_update.return_value.get.return_value = MagicMock(pk=10, is_active=True)

            # Reviewer grant revoked under lock
            mock_access_mgr.select_for_update.return_value.filter.return_value.first.return_value = MagicMock(is_active=False)

            with self.assertRaises(OfferWorkflowError) as cm:
                decide_release_request(2, 99, "approve", "Looks good")
            self.assertEqual(cm.exception.code, "client_access_required")

    def test_ongoing_visit_blocks_approval(self):
        from unittest.mock import MagicMock, patch
        from .services import decide_release_request, OfferWorkflowError

        with patch("django.contrib.auth.get_user_model") as mock_user_cls, \
             patch("apps.assignments.services.can_review_releases", return_value=True), \
             patch("apps.assignments.services.AssignmentReleaseRequest.objects") as mock_req_mgr, \
             patch("apps.assignments.services.Account.objects") as mock_acc_mgr, \
             patch("apps.assignments.services.CollectorProfile.objects") as mock_prof_mgr, \
             patch("apps.assignments.services.AccountAssignment.objects") as mock_assign_mgr, \
             patch("apps.assignments.services.Client.objects") as mock_client_mgr, \
             patch("apps.assignments.services.ClientAccess.objects") as mock_access_mgr, \
             patch("apps.assignments.services.transaction.atomic", side_effect=_mock_atomic):

            reviewer = MagicMock(pk=2, is_active=True)
            mock_user_cls.return_value.objects.filter.return_value.first.return_value = reviewer

            mock_req_mgr.filter.return_value.values.return_value.first.return_value = {
                "assignment_id": 10,
                "assignment__account_id": 20,
                "assignment__collector_id": 5,
                "requested_by_id": 1,
            }

            mock_acc_mgr.select_for_update.return_value.get.return_value = MagicMock(pk=20, client_id=10)
            mock_prof_mgr.select_for_update.return_value.select_related.return_value.get.return_value = MagicMock(pk=5)

            assignment = MagicMock(pk=10, status="active")
            # Ongoing visit in progress
            assignment.activities.filter.return_value.exists.return_value = True
            mock_assign_mgr.select_for_update.return_value.get.return_value = assignment

            mock_client_mgr.select_for_update.return_value.get.return_value = MagicMock(pk=10, is_active=True)
            mock_access_mgr.select_for_update.return_value.filter.return_value.first.return_value = MagicMock(is_active=True)

            req = MagicMock(pk=99, status="requested", created_at=timezone.now())
            mock_req_mgr.select_for_update.return_value.get.return_value = req

            with self.assertRaises(OfferWorkflowError) as cm:
                decide_release_request(2, 99, "approve", "Looks good")
            self.assertEqual(cm.exception.code, "visit_in_progress")

    def test_approval_cancels_outstanding_tasks_and_releases_assignment(self):
        from unittest.mock import MagicMock, patch
        from .services import decide_release_request

        with patch("django.contrib.auth.get_user_model") as mock_user_cls, \
             patch("apps.assignments.services.can_review_releases", return_value=True), \
             patch("apps.assignments.services.AssignmentReleaseRequest.objects") as mock_req_mgr, \
             patch("apps.assignments.services.Account.objects") as mock_acc_mgr, \
             patch("apps.assignments.services.CollectorProfile.objects") as mock_prof_mgr, \
             patch("apps.assignments.services.AccountAssignment.objects") as mock_assign_mgr, \
             patch("apps.assignments.services.Client.objects") as mock_client_mgr, \
             patch("apps.assignments.services.ClientAccess.objects") as mock_access_mgr, \
             patch("apps.assignments.services.append_audit_event") as mock_audit, \
             patch("apps.assignments.services.transaction.atomic", side_effect=_mock_atomic):

            reviewer = MagicMock(pk=2, is_active=True)
            mock_user_cls.return_value.objects.filter.return_value.first.return_value = reviewer

            mock_req_mgr.filter.return_value.values.return_value.first.return_value = {
                "assignment_id": 10,
                "assignment__account_id": 20,
                "assignment__collector_id": 5,
                "requested_by_id": 1,
            }

            acc = MagicMock(pk=20, client_id=10)
            mock_acc_mgr.select_for_update.return_value.get.return_value = acc
            mock_prof_mgr.select_for_update.return_value.select_related.return_value.get.return_value = MagicMock(pk=5)

            assignment = MagicMock(pk=10, status="active", next_action_due_at="2026-10-15T00:00:00Z")
            assignment.activities.filter.return_value.exists.return_value = False
            task_pending = MagicMock(pk=101, status="pending")
            task_requested = MagicMock(pk=102, status="requested")
            assignment.follow_up_tasks.select_for_update.return_value.filter.return_value = [task_pending, task_requested]
            mock_assign_mgr.select_for_update.return_value.get.return_value = assignment

            mock_client_mgr.select_for_update.return_value.get.return_value = MagicMock(pk=10, is_active=True)
            mock_access_mgr.select_for_update.return_value.filter.return_value.first.return_value = MagicMock(is_active=True)

            req = MagicMock(pk=99, status="requested", created_at=timezone.now())
            mock_req_mgr.select_for_update.return_value.get.return_value = req

            result = decide_release_request(2, 99, "approve", "Approved by lead")
            self.assertTrue(result["saved"])
            self.assertTrue(result["changed"])
            self.assertEqual(result["status"], "approved")

            # Check request updated
            self.assertEqual(req.status, "approved")
            self.assertEqual(req.reviewed_by, reviewer)
            self.assertEqual(req.review_reason, "Approved by lead")

            # Check assignment released and next_action_due_at cleared
            self.assertEqual(assignment.status, "released")
            self.assertIsNone(assignment.next_action_due_at)

            # Check tasks cancelled
            self.assertEqual(task_pending.status, "cancelled")
            self.assertEqual(task_requested.status, "cancelled")

            # Verify audit events emitted
            self.assertTrue(mock_audit.called)

    def test_rejection_preserves_assignment_and_tasks(self):
        from unittest.mock import MagicMock, patch
        from .services import decide_release_request

        with patch("django.contrib.auth.get_user_model") as mock_user_cls, \
             patch("apps.assignments.services.can_review_releases", return_value=True), \
             patch("apps.assignments.services.AssignmentReleaseRequest.objects") as mock_req_mgr, \
             patch("apps.assignments.services.Account.objects") as mock_acc_mgr, \
             patch("apps.assignments.services.CollectorProfile.objects") as mock_prof_mgr, \
             patch("apps.assignments.services.AccountAssignment.objects") as mock_assign_mgr, \
             patch("apps.assignments.services.Client.objects") as mock_client_mgr, \
             patch("apps.assignments.services.ClientAccess.objects") as mock_access_mgr, \
             patch("apps.assignments.services.append_audit_event") as mock_audit, \
             patch("apps.assignments.services.transaction.atomic", side_effect=_mock_atomic):

            reviewer = MagicMock(pk=2, is_active=True)
            mock_user_cls.return_value.objects.filter.return_value.first.return_value = reviewer

            mock_req_mgr.filter.return_value.values.return_value.first.return_value = {
                "assignment_id": 10,
                "assignment__account_id": 20,
                "assignment__collector_id": 5,
                "requested_by_id": 1,
            }

            acc = MagicMock(pk=20, client_id=10)
            mock_acc_mgr.select_for_update.return_value.get.return_value = acc
            mock_prof_mgr.select_for_update.return_value.select_related.return_value.get.return_value = MagicMock(pk=5)

            assignment = MagicMock(pk=10, status="active")
            mock_assign_mgr.select_for_update.return_value.get.return_value = assignment

            mock_client_mgr.select_for_update.return_value.get.return_value = MagicMock(pk=10, is_active=True)
            mock_access_mgr.select_for_update.return_value.filter.return_value.first.return_value = MagicMock(is_active=True)

            req = MagicMock(pk=99, status="requested", created_at=timezone.now())
            mock_req_mgr.select_for_update.return_value.get.return_value = req

            result = decide_release_request(2, 99, "reject", "Cannot release before scheduled payment date.")
            self.assertTrue(result["saved"])
            self.assertTrue(result["changed"])
            self.assertEqual(result["status"], "rejected")

            # Check request updated
            self.assertEqual(req.status, "rejected")
            self.assertEqual(req.reviewed_by, reviewer)
            self.assertEqual(req.review_reason, "Cannot release before scheduled payment date.")

            # Assignment status should NOT be changed
            self.assertEqual(assignment.status, "active")
            assignment.save.assert_not_called()


class ReleaseRequestDatabaseTests(TestCase):
    """
    Real database verification for the release request workflow.
    Requires an accessible disposable PostgreSQL/PostGIS test database.
    Unrun if session lacks CREATE DATABASE or connection privileges.
    """
    def setUp(self):
        from apps.accounts.models import Client as CreditorClient
        from django.contrib.auth.models import Permission
        from apps.user.models import ClientAccess

        self.collector_user, self.profile, self.creditor, self.account = offer_fixture()
        self.creditor.allow_collector_release = True
        self.creditor.save(update_fields=['allow_collector_release'])

        self.assignment = AccountAssignment.objects.create(
            account=self.account,
            collector=self.profile,
            target_address=self.account.collection_address,
            target_position=self.account.collection_address.position,
            commission_rule=self.account.commission_rule,
            status='active',
            first_action_due_at=timezone.now() + timedelta(hours=24),
            next_action_due_at=timezone.now() + timedelta(hours=24),
            policy_snapshot={},
        )

        self.reviewer = get_user_model().objects.create_user(username='release-reviewer-db')
        perm = Permission.objects.get(codename='review_release_requests', content_type__app_label='assignments')
        self.reviewer.user_permissions.add(perm)
        ClientAccess.objects.create(user=self.reviewer, client=self.creditor, is_active=True)

    def test_database_constraints(self):
        from django.db import IntegrityError, transaction
        from .models import AssignmentReleaseRequest
        import uuid

        # 1. Valid pending release request succeeds
        req1 = AssignmentReleaseRequest.objects.create(
            assignment=self.assignment,
            requested_by=self.collector_user,
            event_id=uuid.uuid4(),
            reason="Initial release reason",
            status="requested",
        )
        self.assertIsNotNone(req1.pk)

        # 2. Duplicate pending request on same assignment -> IntegrityError (unique_pending_release_request_per_assignment)
        with self.assertRaises(IntegrityError) as cm:
            with transaction.atomic():
                AssignmentReleaseRequest.objects.create(
                    assignment=self.assignment,
                    requested_by=self.collector_user,
                    event_id=uuid.uuid4(),
                    reason="Second pending request on same assignment",
                    status="requested",
                )
        self.assertEqual(cm.exception.__cause__.diag.constraint_name, "unique_pending_release_request_per_assignment")

        # Delete req1 so self.assignment has no pending release request for subsequent constraint tests
        req1.delete()

        # 3. Invalid status choice -> IntegrityError (release_request_valid_status)
        # Drop release_request_review_evidence within the savepoint so PostgreSQL specifically verifies
        # release_request_valid_status rather than release_request_review_evidence rejecting the row first.
        with self.assertRaises(IntegrityError) as cm:
            with transaction.atomic():
                from django.db import connection
                with connection.cursor() as cursor:
                    cursor.execute(
                        "ALTER TABLE assignments_assignmentreleaserequest "
                        "DROP CONSTRAINT release_request_review_evidence"
                    )
                AssignmentReleaseRequest.objects.create(
                    assignment=self.assignment,
                    requested_by=self.collector_user,
                    event_id=uuid.uuid4(),
                    reason="Invalid status attempt",
                    status="cancelled",
                )
        self.assertEqual(cm.exception.__cause__.diag.constraint_name, "release_request_valid_status")

        # 4. Pending request with reviewer evidence -> IntegrityError (release_request_review_evidence)
        # No other pending request exists on self.assignment, so duplicate-pending cannot mask this
        with self.assertRaises(IntegrityError) as cm:
            with transaction.atomic():
                AssignmentReleaseRequest.objects.create(
                    assignment=self.assignment,
                    requested_by=self.collector_user,
                    event_id=uuid.uuid4(),
                    reason="Pending with reviewer details",
                    status="requested",
                    reviewed_by=self.reviewer,
                    reviewed_at=timezone.now(),
                )
        self.assertEqual(cm.exception.__cause__.diag.constraint_name, "release_request_review_evidence")

        # 5. Finalized request without reviewer evidence -> IntegrityError (release_request_review_evidence)
        # Status is approved (not requested), so duplicate-pending does not apply
        with self.assertRaises(IntegrityError) as cm:
            with transaction.atomic():
                AssignmentReleaseRequest.objects.create(
                    assignment=self.assignment,
                    requested_by=self.collector_user,
                    event_id=uuid.uuid4(),
                    reason="Approved without reviewer details",
                    status="approved",
                    reviewed_by=None,
                    reviewed_at=None,
                )
        self.assertEqual(cm.exception.__cause__.diag.constraint_name, "release_request_review_evidence")

        # 6. Rejected request without review note -> IntegrityError (release_request_rejected_reason)
        # Status is rejected with reviewer details, so review-evidence and duplicate-pending are satisfied
        with self.assertRaises(IntegrityError) as cm:
            with transaction.atomic():
                AssignmentReleaseRequest.objects.create(
                    assignment=self.assignment,
                    requested_by=self.collector_user,
                    event_id=uuid.uuid4(),
                    reason="Rejected without review reason",
                    status="rejected",
                    reviewed_by=self.reviewer,
                    reviewed_at=timezone.now(),
                    review_reason="",
                )
        self.assertEqual(cm.exception.__cause__.diag.constraint_name, "release_request_rejected_reason")

        # 7. Request with empty reason -> IntegrityError (release_request_reason_not_empty)
        # Uses approved status with complete reviewer details so duplicate-pending and review-evidence cannot mask this
        with self.assertRaises(IntegrityError) as cm:
            with transaction.atomic():
                AssignmentReleaseRequest.objects.create(
                    assignment=self.assignment,
                    requested_by=self.collector_user,
                    event_id=uuid.uuid4(),
                    reason="",
                    status="approved",
                    reviewed_by=self.reviewer,
                    reviewed_at=timezone.now(),
                    review_reason="Approved note",
                )
        self.assertEqual(cm.exception.__cause__.diag.constraint_name, "release_request_reason_not_empty")

    def test_approval_transaction_ends_ownership_and_cancels_tasks(self):
        from apps.activities.models import FollowUpTask
        from .models import AssignmentReleaseRequest
        from .services import decide_release_request
        import uuid

        req = AssignmentReleaseRequest.objects.create(
            assignment=self.assignment,
            requested_by=self.collector_user,
            event_id=uuid.uuid4(),
            reason="Cannot service area",
            status="requested",
        )

        task_requested = FollowUpTask.objects.create(
            account=self.account, assignment=self.assignment, assignee=self.collector_user,
            kind="revisit", due_at=timezone.now() + timedelta(days=2), status="requested"
        )
        task_pending = FollowUpTask.objects.create(
            account=self.account, assignment=self.assignment, assignee=self.collector_user,
            kind="call", due_at=timezone.now() + timedelta(days=2), status="pending"
        )
        task_completed = FollowUpTask.objects.create(
            account=self.account, assignment=self.assignment, assignee=self.collector_user,
            kind="review", due_at=timezone.now() - timedelta(days=1), status="completed"
        )

        res = decide_release_request(self.reviewer.pk, req.pk, "approve", "Approved by team lead")
        self.assertTrue(res["saved"])
        self.assertTrue(res["changed"])
        self.assertEqual(res["status"], "approved")

        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, "released")
        self.assertIsNotNone(self.assignment.ended_at)
        self.assertIsNone(self.assignment.next_action_due_at)

        task_requested.refresh_from_db()
        task_pending.refresh_from_db()
        task_completed.refresh_from_db()
        self.assertEqual(task_requested.status, "cancelled")
        self.assertEqual(task_pending.status, "cancelled")
        self.assertEqual(task_completed.status, "completed")

    def test_ongoing_visit_blocks_approval_and_rolls_back(self):
        from apps.activities.models import FieldActivity
        from .models import AssignmentReleaseRequest
        from .services import decide_release_request, OfferWorkflowError
        import uuid

        req = AssignmentReleaseRequest.objects.create(
            assignment=self.assignment,
            requested_by=self.collector_user,
            event_id=uuid.uuid4(),
            reason="Cannot service area",
            status="requested",
        )

        # FieldActivity fixture includes location_issue_reason to satisfy activity_missing_gps_reason constraint
        FieldActivity.objects.create(
            account=self.account, assignment=self.assignment, recorded_by=self.collector_user,
            kind="visit", status="in_progress", occurred_at=timezone.now(),
            location_issue_reason="Indoor facility without GPS fix",
        )

        with self.assertRaises(OfferWorkflowError) as cm:
            decide_release_request(self.reviewer.pk, req.pk, "approve", "Approved")
        self.assertEqual(cm.exception.code, "visit_in_progress")

        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, "active")
        req.refresh_from_db()
        self.assertEqual(req.status, "requested")

    def test_rejection_preserves_assignment_and_tasks(self):
        from apps.activities.models import FollowUpTask
        from .models import AssignmentReleaseRequest
        from .services import decide_release_request
        import uuid

        req = AssignmentReleaseRequest.objects.create(
            assignment=self.assignment,
            requested_by=self.collector_user,
            event_id=uuid.uuid4(),
            reason="Need release",
            status="requested",
        )
        task = FollowUpTask.objects.create(
            account=self.account, assignment=self.assignment, assignee=self.collector_user,
            kind="revisit", due_at=timezone.now() + timedelta(days=2), status="pending"
        )

        res = decide_release_request(self.reviewer.pk, req.pk, "reject", "Please finish assigned visit first.")
        self.assertTrue(res["saved"])
        self.assertEqual(res["status"], "rejected")

        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, "active")
        task.refresh_from_db()
        self.assertEqual(task.status, "pending")
        req.refresh_from_db()
        self.assertEqual(req.status, "rejected")
        self.assertEqual(req.review_reason, "Please finish assigned visit first.")

    def test_retry_after_approval_acknowledges_without_restoring_access(self):
        from .models import AssignmentReleaseRequest
        from .services import decide_release_request, request_assignment_release
        import uuid

        event_uuid = uuid.uuid4()
        req = AssignmentReleaseRequest.objects.create(
            assignment=self.assignment,
            requested_by=self.collector_user,
            event_id=event_uuid,
            reason="Moving out of city",
            status="requested",
        )

        decide_release_request(self.reviewer.pk, req.pk, "approve", "Approved")
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, "released")

        # Collector device retries with same event_id and reason
        res = request_assignment_release(
            self.collector_user.pk,
            self.assignment.pk,
            {"event_id": str(event_uuid), "reason": "Moving out of city"}
        )
        self.assertTrue(res["saved"])
        self.assertFalse(res["created"])
        self.assertEqual(res["request"].pk, req.pk)

        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, "released")


class ReleaseRequestConcurrencyTests(TransactionTestCase):
    """
    Concurrency verification for competing Back Office release decisions.
    Requires a multi-connection test database.
    Unrun if session lacks CREATE DATABASE or connection privileges.
    """
    def test_competing_reviewers_produce_one_final_decision(self):
        from .models import AssignmentReleaseRequest
        from .services import decide_release_request, OfferWorkflowError
        from django.contrib.auth.models import Permission
        from apps.user.models import ClientAccess
        import uuid

        user, profile, creditor, account = offer_fixture()
        creditor.allow_collector_release = True
        creditor.save(update_fields=['allow_collector_release'])

        assignment = AccountAssignment.objects.create(
            account=account, collector=profile, target_address=account.collection_address,
            target_position=account.collection_address.position, commission_rule=account.commission_rule,
            status='active', first_action_due_at=timezone.now() + timedelta(hours=24), policy_snapshot={}
        )

        req = AssignmentReleaseRequest.objects.create(
            assignment=assignment, requested_by=user, event_id=uuid.uuid4(),
            reason="Moving away", status="requested"
        )

        perm = Permission.objects.get(codename='review_release_requests', content_type__app_label='assignments')

        rev1 = get_user_model().objects.create_user(username='reviewer-concurrency-1')
        rev1.user_permissions.add(perm)
        ClientAccess.objects.create(user=rev1, client=creditor, is_active=True)

        rev2 = get_user_model().objects.create_user(username='reviewer-concurrency-2')
        rev2.user_permissions.add(perm)
        ClientAccess.objects.create(user=rev2, client=creditor, is_active=True)

        barrier = Barrier(2, timeout=10)

        def review_decision(reviewer_id, decision, reason):
            close_old_connections()
            try:
                barrier.wait()
                try:
                    return decide_release_request(reviewer_id, req.pk, decision, reason)
                except OfferWorkflowError as exc:
                    return {"error": exc.code}
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(review_decision, rev1.pk, "approve", "Approved by rev1")
            f2 = pool.submit(review_decision, rev2.pk, "reject", "Rejected by rev2")
            results = [f1.result(timeout=20), f2.result(timeout=20)]

        successes = [r for r in results if r.get("saved") is True and r.get("changed") is True]
        conflicts = [r for r in results if r.get("error") == "review_conflict"]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(conflicts), 1)

        req.refresh_from_db()
        self.assertIn(req.status, ["approved", "rejected"])


class AccountAssignmentPureTests(SimpleTestCase):
    def test_routes_and_urls_exist(self):
        self.assertEqual(reverse("backoffice_assignments"), "/back-office/assignments/")
        self.assertEqual(reverse("backoffice_account_assignment_detail", args=[42]), "/back-office/assignments/42/")
        self.assertEqual(reverse("assign_account_form", args=[42]), "/back-office/assignments/42/assign/")

    def test_can_manage_assignments(self):
        from .services import can_manage_assignments
        self.assertFalse(can_manage_assignments(None))
        self.assertFalse(can_manage_assignments(SimpleNamespace(is_authenticated=False, is_active=True)))
        self.assertFalse(can_manage_assignments(SimpleNamespace(is_authenticated=True, is_active=False)))

        # Superuser
        superuser = SimpleNamespace(is_authenticated=True, is_active=True, is_superuser=True)
        self.assertTrue(can_manage_assignments(superuser))

        # User with permission
        def mock_has_perm(perm):
            return perm == "assignments.manage_account_assignments"

        staff = SimpleNamespace(is_authenticated=True, is_active=True, is_superuser=False, has_perm=mock_has_perm)
        self.assertTrue(can_manage_assignments(staff))

        # User without permission
        non_staff = SimpleNamespace(is_authenticated=True, is_active=True, is_superuser=False, has_perm=lambda p: False)
        self.assertFalse(can_manage_assignments(non_staff))

    def test_compute_ownership_token_deterministic(self):
        from .services import compute_ownership_token

        class MockAssignmentQuerySet:
            def __init__(self, items):
                self.items = items

            def filter(self, **kwargs):
                status = kwargs.get("status")
                filtered = [x for x in self.items if x.status == status]
                return MockAssignmentQuerySet(filtered)

            def order_by(self, field):
                if field == "-pk":
                    return MockAssignmentQuerySet(sorted(self.items, key=lambda x: x.pk, reverse=True))
                return self

            def first(self):
                return self.items[0] if self.items else None

            def count(self):
                return len(self.items)

        # Empty assignments: '0:0:0'
        acc0 = SimpleNamespace(assignments=MockAssignmentQuerySet([]))
        self.assertEqual(compute_ownership_token(acc0), "0:0:0")

        # One active assignment (pk=1): '1:1:1'
        a1 = SimpleNamespace(pk=1, status="active")
        acc1 = SimpleNamespace(assignments=MockAssignmentQuerySet([a1]))
        self.assertEqual(compute_ownership_token(acc1), "1:1:1")

        # Two assignments: a1 reassigned, a2 active (pk=2): '2:2:2'
        a1_reassigned = SimpleNamespace(pk=1, status="reassigned")
        a2 = SimpleNamespace(pk=2, status="active")
        acc2 = SimpleNamespace(assignments=MockAssignmentQuerySet([a1_reassigned, a2]))
        self.assertEqual(compute_ownership_token(acc2), "2:2:2")

        # Two assignments: a2 released (no active): '0:2:2'
        a2_released = SimpleNamespace(pk=2, status="released")
        acc3 = SimpleNamespace(assignments=MockAssignmentQuerySet([a1_reassigned, a2_released]))
        self.assertEqual(compute_ownership_token(acc3), "0:2:2")

        # Detects if initially unassigned account (0:0:0) was assigned & released while form open (0:2:2)
        self.assertNotEqual(compute_ownership_token(acc0), compute_ownership_token(acc3))

    def test_assign_or_reassign_input_validation(self):
        from .services import assign_or_reassign_account, OfferWorkflowError

        # Invalid UUID
        with self.assertRaises(OfferWorkflowError) as cm:
            assign_or_reassign_account(1, 1, 1, "Reason", "not-a-uuid", "0:0:0")
        self.assertEqual(cm.exception.code, "invalid_event_id")

        valid_uuid = "12345678-1234-5678-1234-567812345678"

        # Blank reason
        with self.assertRaises(OfferWorkflowError) as cm:
            assign_or_reassign_account(1, 1, 1, "   ", valid_uuid, "0:0:0")
        self.assertEqual(cm.exception.code, "reason_required")

        # Reason too long
        with self.assertRaises(OfferWorkflowError) as cm:
            assign_or_reassign_account(1, 1, 1, "x" * 5001, valid_uuid, "0:0:0")
        self.assertEqual(cm.exception.code, "reason_too_long")

        # Blank ownership token
        with self.assertRaises(OfferWorkflowError) as cm:
            assign_or_reassign_account(1, 1, 1, "Valid reason", valid_uuid, "   ")
        self.assertEqual(cm.exception.code, "ownership_token_required")

        # Invalid collector id
        with self.assertRaises(OfferWorkflowError) as cm:
            assign_or_reassign_account(1, 1, "not-an-int", "Valid reason", valid_uuid, "0:0:0")
        self.assertEqual(cm.exception.code, "invalid_collector")

    def test_release_request_superseded_validation(self):
        from .models import AssignmentReleaseRequest

        req = AssignmentReleaseRequest(
            status="superseded",
            reason="Want release",
            review_reason="",
        )
        with self.assertRaises(ValidationError):
            req.clean()

        user = get_user_model()(pk=99, username="reviewer-check")
        now = timezone.now()
        req_valid = AssignmentReleaseRequest(
            status="superseded",
            reason="Want release",
            review_reason="Superseded by account reassignment.",
            reviewed_by=user,
            reviewed_at=now,
        )
        req_valid.clean()

    def test_audit_reason_length_enforcement(self):
        from apps.audit.services import validate_audit_changes

        long_reason = "X" * 101
        valid_reason = "X" * 100

        # All assignment/release audit actions require scalar reason <= 100
        for action, payload in [
            ("release_superseded", {"status": {"before": "requested", "after": "superseded"}, "reason": long_reason}),
            ("assignment_reassigned", {"status": {"before": "active", "after": "reassigned"}, "reason": long_reason}),
            ("account_reassigned", {"status": {"before": "open", "after": "open"}, "previous_assignment_id": 1, "new_assignment_id": 2, "previous_collector": "c1", "new_collector": "c2", "reason": long_reason, "cancelled_followups_count": 0, "superseded_release_request_id": None, "withdrawn_offers_count": 0}),
            ("account_assigned", {"status": {"before": "open", "after": "open"}, "new_assignment_id": 2, "collector": "c2", "reason": long_reason, "withdrawn_offers_count": 0}),
        ]:
            with self.subTest(action=action):
                with self.assertRaises(ValidationError):
                    validate_audit_changes(action, payload)

        # Bounded reason <= 100 passes
        for action, payload in [
            ("release_superseded", {"status": {"before": "requested", "after": "superseded"}, "reason": valid_reason}),
            ("assignment_reassigned", {"status": {"before": "active", "after": "reassigned"}, "reason": valid_reason}),
            ("account_reassigned", {"status": {"before": "open", "after": "open"}, "previous_assignment_id": 1, "new_assignment_id": 2, "previous_collector": "c1", "new_collector": "c2", "reason": valid_reason, "cancelled_followups_count": 0, "superseded_release_request_id": None, "withdrawn_offers_count": 0}),
            ("account_assigned", {"status": {"before": "open", "after": "open"}, "new_assignment_id": 2, "collector": "c2", "reason": valid_reason, "withdrawn_offers_count": 0}),
        ]:
            with self.subTest(action=action, test="valid"):
                validate_audit_changes(action, payload)

    def test_assign_or_reassign_permission_check(self):
        from .services import assign_or_reassign_account, OfferWorkflowError
        import uuid

        # Mock reviewer lookup returning user without permission
        mock_user = SimpleNamespace(pk=1, is_authenticated=True, is_active=True, is_superuser=False, has_perm=lambda p: False)
        with patch("apps.assignments.services.transaction.atomic"):
            with patch("django.contrib.auth.get_user_model") as mock_model:
                mock_model.return_value.objects.filter.return_value.first.return_value = mock_user
                with self.assertRaises(OfferWorkflowError) as cm:
                    assign_or_reassign_account(1, 1, 1, "Reason", str(uuid.uuid4()), "0:0:0")
                self.assertEqual(cm.exception.code, "permission_required")
                self.assertEqual(cm.exception.status, 403)

    def test_self_assignment_and_backoffice_error_precedence(self):
        from unittest.mock import MagicMock, patch
        from .services import assign_or_reassign_account, OfferWorkflowError
        import uuid

        with patch("django.contrib.auth.get_user_model") as mock_user_cls, \
             patch("apps.assignments.services.can_manage_assignments", return_value=True), \
             patch("apps.assignments.services.has_backoffice_access", return_value=True), \
             patch("apps.assignments.services.Account.objects") as mock_acc_mgr, \
             patch("apps.assignments.services.CollectorProfile.objects") as mock_prof_mgr, \
             patch("apps.assignments.services.AccountAssignment.objects") as mock_assign_mgr, \
             patch("apps.assignments.services.Client.objects") as mock_client_mgr, \
             patch("apps.assignments.services.ClientAccess.objects") as mock_grant_mgr, \
             patch("apps.assignments.services.CustomerAddress.objects") as mock_addr_mgr, \
             patch("apps.assignments.services.CommissionRule.objects") as mock_rule_mgr, \
             patch("apps.assignments.services.validate_account_for_offers"), \
             patch("apps.assignments.services.compute_ownership_token", return_value="0:0:0"), \
             patch("apps.activities.models.FieldActivity.objects") as mock_act_mgr, \
             patch("apps.assignments.services.transaction.atomic", side_effect=_mock_atomic):

            reviewer = MagicMock(pk=10, is_active=True)
            mock_user_cls.return_value.objects.filter.return_value.first.return_value = reviewer

            account = MagicMock(pk=20, client_id=1, collection_address_id=5, commission_rule_id=7, source_system="TEST", status="open")
            mock_acc_mgr.select_for_update.return_value.filter.return_value.first.return_value = account

            mock_assign_mgr.filter.return_value.values_list.return_value.first.return_value = None
            account.assignments.select_for_update.return_value.filter.return_value.first.return_value = None

            client = MagicMock(pk=1, is_active=True)
            mock_client_mgr.select_for_update.return_value.filter.return_value.first.return_value = client

            reviewer_grant = MagicMock(user_id=10, client_id=1, is_active=True)
            mock_grant_mgr.select_for_update.return_value.filter.return_value.order_by.return_value = [reviewer_grant]

            # Replay assignment does not exist
            mock_assign_mgr.select_for_update.return_value.filter.return_value.first.return_value = None

            addr = MagicMock(pk=5, verification_status="verified", position=MagicMock())
            mock_addr_mgr.select_for_update.return_value.filter.return_value.first.return_value = addr

            rule = MagicMock(pk=7, is_active=True)
            mock_rule_mgr.select_for_update.return_value.filter.return_value.first.return_value = rule

            mock_act_mgr.filter.return_value.exists.return_value = False

            # Case A: Reviewer attempts to assign to themselves (recipient user_id == reviewer.pk == 10)
            self_profile = MagicMock(pk=50, user_id=10, user=MagicMock(is_active=True), approval_status="approved")
            mock_prof_mgr.select_for_update.return_value.select_related.return_value.filter.return_value.order_by.return_value = [self_profile]

            with self.assertRaises(OfferWorkflowError) as cm:
                assign_or_reassign_account(10, 20, 50, "Assign myself", str(uuid.uuid4()), "0:0:0")
            self.assertEqual(cm.exception.code, "cannot_assign_self")
            self.assertEqual(cm.exception.status, 403)

            # Case B: Reviewer attempts to assign to another staff member (recipient user_id == 11, has_backoffice_access=True)
            other_staff_profile = MagicMock(pk=51, user_id=11, user=MagicMock(is_active=True), approval_status="approved")
            mock_prof_mgr.select_for_update.return_value.select_related.return_value.filter.return_value.order_by.return_value = [other_staff_profile]

            with self.assertRaises(OfferWorkflowError) as cm:
                assign_or_reassign_account(10, 20, 51, "Assign colleague", str(uuid.uuid4()), "0:0:0")
            self.assertEqual(cm.exception.code, "ordinary_collector_required")
            self.assertEqual(cm.exception.status, 403)

    def test_lock_order_and_reviewer_client_access_before_replay(self):
        from unittest.mock import MagicMock, patch
        from .services import assign_or_reassign_account, OfferWorkflowError
        import uuid

        with patch("django.contrib.auth.get_user_model") as mock_user_cls, \
             patch("apps.assignments.services.can_manage_assignments", return_value=True), \
             patch("apps.assignments.services.Account.objects") as mock_acc_mgr, \
             patch("apps.assignments.services.CollectorProfile.objects") as mock_prof_mgr, \
             patch("apps.assignments.services.AccountAssignment.objects") as mock_assign_mgr, \
             patch("apps.assignments.services.Client.objects") as mock_client_mgr, \
             patch("apps.assignments.services.ClientAccess.objects") as mock_grant_mgr, \
             patch("apps.assignments.services.transaction.atomic", side_effect=_mock_atomic):

            reviewer = MagicMock(pk=10, is_active=True)
            mock_user_cls.return_value.objects.filter.return_value.first.return_value = reviewer

            account = MagicMock(pk=20, client_id=1)
            mock_acc_mgr.select_for_update.return_value.filter.return_value.first.return_value = account

            mock_assign_mgr.filter.return_value.values_list.return_value.first.return_value = None
            account.assignments.select_for_update.return_value.filter.return_value.first.return_value = None

            recipient_profile = MagicMock(pk=50, user_id=25, user=MagicMock(is_active=True), approval_status="approved")
            mock_prof_mgr.select_for_update.return_value.select_related.return_value.filter.return_value.order_by.return_value = [recipient_profile]

            client = MagicMock(pk=1, is_active=True)
            mock_client_mgr.select_for_update.return_value.filter.return_value.first.return_value = client

            # Reviewer grant is revoked or missing
            mock_grant_mgr.select_for_update.return_value.filter.return_value.order_by.return_value = []

            # Even if an existing assignment with identical token exists
            ev_id = str(uuid.uuid4())
            existing = MagicMock(account_id=20, collector_id=50, policy_snapshot={"assigned_by_id": 10, "assignment_reason": "Reason", "submitted_ownership_token": "0:0:0"})
            mock_assign_mgr.select_for_update.return_value.filter.return_value.first.return_value = existing

            with self.assertRaises(OfferWorkflowError) as cm:
                assign_or_reassign_account(10, 20, 50, "Reason", ev_id, "0:0:0")
            # Must raise reviewer_client_access_required, not return existing replay
            self.assertEqual(cm.exception.code, "reviewer_client_access_required")
            self.assertEqual(cm.exception.status, 403)

    def test_ongoing_visit_warning_in_template(self):
        from django.template.loader import render_to_string
        from django.test import RequestFactory

        req = RequestFactory().get("/back-office/assignments/1/")
        req.user = SimpleNamespace(is_authenticated=True, username="staff", get_full_name=lambda: "Staff")

        context_blocked = {
            "account": SimpleNamespace(pk=1, reference="ACC-001", customer=SimpleNamespace(display_name="Acme"), client=SimpleNamespace(name="ClientA"), balance_snapshot=100.0, currency="PHP", collection_address=SimpleNamespace(address_line="123 St", territory=None), commission_rule=SimpleNamespace(name="Rule", code="R1")),
            "current_assignment": None,
            "assignment_history": [],
            "ownership_token": "0:0:0",
            "event_id": "12345678-1234-5678-1234-567812345678",
            "collectors": [{"id": 1, "username": "col1", "full_name": "Collector One", "availability": "online", "workload": 0, "recent_acceptances": 0, "distance_km": None, "location_captured_at": None}],
            "has_ongoing_visit": True,
        }
        rendered = render_to_string("backoffice/assignment_detail.html", context_blocked, request=req)
        self.assertIn("bo-decision-warning", rendered)
        self.assertIn("Assignment / transfer blocked: a visit is currently in progress", rendered)
        self.assertIn("disabled", rendered)

        context_unblocked = dict(context_blocked, has_ongoing_visit=False)
        rendered_unblocked = render_to_string("backoffice/assignment_detail.html", context_unblocked, request=req)
        self.assertNotIn("Assignment / transfer blocked: a visit is currently in progress", rendered_unblocked)

    def test_account_list_template_renders_active_assignment_only(self):
        from django.template.loader import render_to_string
        from django.test import RequestFactory

        req = RequestFactory().get("/back-office/assignments/")
        req.user = SimpleNamespace(is_authenticated=True, username="staff", get_full_name=lambda: "Staff")

        # Account with active assignment prefetched to active_assignments
        collector_user = SimpleNamespace(username="active-collector", get_full_name=lambda: "Active Collector")
        active_assignment = SimpleNamespace(pk=2, status="active", collector=SimpleNamespace(user=collector_user))

        acc = SimpleNamespace(
            pk=10,
            reference="ACC-ACTIVE",
            customer=SimpleNamespace(display_name="Customer 1"),
            client=SimpleNamespace(name="Client 1"),
            collection_address=SimpleNamespace(address_line="Address", territory=None),
            currency="PHP",
            balance_snapshot=500.0,
            active_assignments=[active_assignment],
        )

        rendered = render_to_string("backoffice/assignments.html", {
            "accounts": [acc],
            "page_obj": SimpleNamespace(paginator=SimpleNamespace(count=1, num_pages=1), number=1, has_other_pages=False, has_previous=False, has_next=False),
            "assignment_state": "assigned",
            "assignment_counts": {"unassigned": 0, "assigned": 1, "all": 1},
            "search_query": "",
        }, request=req)
        self.assertIn("Active Collector", rendered)
        self.assertIn("bo-status-badge is-assigned", rendered)
        self.assertIn("Manage / Transfer", rendered)

        # Account with no active assignment (empty active_assignments)
        acc_empty = SimpleNamespace(
            pk=11,
            reference="ACC-EMPTY",
            customer=SimpleNamespace(display_name="Customer 2"),
            client=SimpleNamespace(name="Client 1"),
            collection_address=SimpleNamespace(address_line="Address 2", territory=None),
            currency="PHP",
            balance_snapshot=250.0,
            active_assignments=[],
        )
        rendered_empty = render_to_string("backoffice/assignments.html", {
            "accounts": [acc_empty],
            "page_obj": SimpleNamespace(paginator=SimpleNamespace(count=1, num_pages=1), number=1, has_other_pages=False, has_previous=False, has_next=False),
            "assignment_state": "unassigned",
            "assignment_counts": {"unassigned": 1, "assigned": 0, "all": 1},
            "search_query": "",
        }, request=req)
        self.assertIn("None (Ready for assignment)", rendered_empty)
        self.assertIn("bo-status-badge is-unassigned", rendered_empty)
        self.assertIn("Assign Account", rendered_empty)


class AccountAssignmentWorkflowDatabaseTests(TestCase):
    def setUp(self):
        self.user, self.profile, self.creditor, self.account = offer_fixture(second=True)
        from django.contrib.auth.models import Permission
        self.staff_user = get_user_model().objects.create_user(username="bo-staff-assignment")
        perm = Permission.objects.get(codename="manage_account_assignments", content_type__app_label="assignments")
        self.staff_user.user_permissions.add(perm)
        ClientAccess.objects.create(user=self.staff_user, client=self.creditor, is_active=True)

        self.other_profile = CollectorProfile.objects.exclude(pk=self.profile.pk).first()

    def test_direct_assignment_and_exact_idempotency(self):
        from .services import assign_or_reassign_account, compute_ownership_token, OfferWorkflowError
        import uuid

        token = compute_ownership_token(self.account)
        event_id = str(uuid.uuid4())
        reason = "Initial operational assignment"

        res = assign_or_reassign_account(
            staff_user_id=self.staff_user.pk,
            account_id=self.account.pk,
            collector_id=self.profile.pk,
            reason=reason,
            event_id=event_id,
            submitted_ownership_token=token,
        )
        self.assertTrue(res["saved"])
        self.assertTrue(res["created"])
        self.assertFalse(res["reassigned"])
        assignment = res["assignment"]
        self.assertEqual(assignment.status, "active")
        self.assertEqual(assignment.collector_id, self.profile.pk)
        self.assertEqual(assignment.event_id, uuid.UUID(event_id))
        self.assertEqual(assignment.policy_snapshot["assigned_by_id"], self.staff_user.pk)
        self.assertEqual(assignment.policy_snapshot["assignment_reason"], reason)
        self.assertEqual(assignment.policy_snapshot["submitted_ownership_token"], token)

        # Exact retry returns same assignment without creating duplicates
        retry_res = assign_or_reassign_account(
            staff_user_id=self.staff_user.pk,
            account_id=self.account.pk,
            collector_id=self.profile.pk,
            reason=reason,
            event_id=event_id,
            submitted_ownership_token=token,
        )
        self.assertTrue(retry_res["saved"])
        self.assertFalse(retry_res["created"])
        self.assertEqual(retry_res["assignment"].pk, assignment.pk)

        # Reusing event_id with different reason raises event_conflict
        with self.assertRaises(OfferWorkflowError) as cm:
            assign_or_reassign_account(
                staff_user_id=self.staff_user.pk,
                account_id=self.account.pk,
                collector_id=self.profile.pk,
                reason="Different reason",
                event_id=event_id,
                submitted_ownership_token=token,
            )
        self.assertEqual(cm.exception.code, "event_conflict")

    def test_reassignment_supersedes_pending_release_and_cancels_tasks(self):
        from .services import assign_or_reassign_account, compute_ownership_token
        from .models import AssignmentReleaseRequest
        from apps.activities.models import FollowUpTask
        import uuid

        # Initial assignment
        token0 = compute_ownership_token(self.account)
        ev1 = str(uuid.uuid4())
        res1 = assign_or_reassign_account(
            self.staff_user.pk, self.account.pk, self.profile.pk, "Initial assignment", ev1, token0
        )
        first_assignment = res1["assignment"]

        # Create a pending follow-up task on first_assignment
        task = FollowUpTask.objects.create(
            account=self.account,
            assignment=first_assignment,
            assignee=self.profile.user,
            kind="revisit",
            status="requested",
            due_at=timezone.now() + timedelta(days=2),
        )

        # Create a pending release request on first_assignment
        rel_req = AssignmentReleaseRequest.objects.create(
            assignment=first_assignment,
            requested_by=self.profile.user,
            event_id=uuid.uuid4(),
            reason="Cannot visit area",
            status="requested",
        )

        # Reassign account to other_profile
        token1 = compute_ownership_token(self.account)
        ev2 = str(uuid.uuid4())
        reassign_reason = "Reassigning to collector closer to territory"
        res2 = assign_or_reassign_account(
            self.staff_user.pk, self.account.pk, self.other_profile.pk, reassign_reason, ev2, token1
        )
        self.assertTrue(res2["saved"])
        self.assertTrue(res2["created"])
        self.assertTrue(res2["reassigned"])

        # First assignment is reassigned
        first_assignment.refresh_from_db()
        self.assertEqual(first_assignment.status, "reassigned")
        self.assertEqual(first_assignment.end_reason, reassign_reason)
        self.assertIsNone(first_assignment.next_action_due_at)

        # Task was cancelled
        task.refresh_from_db()
        self.assertEqual(task.status, "cancelled")

        # Release request was superseded
        rel_req.refresh_from_db()
        self.assertEqual(rel_req.status, "superseded")
        self.assertEqual(rel_req.reviewed_by_id, self.staff_user.pk)
        self.assertIn("Superseded by reassignment", rel_req.review_reason)

        # Deciding a superseded release request must raise review_conflict
        from .services import decide_release_request, OfferWorkflowError
        from django.contrib.auth.models import Permission
        self.staff_user.user_permissions.add(
            Permission.objects.get(codename="review_release_requests", content_type__app_label="assignments")
        )
        with self.assertRaises(OfferWorkflowError) as cm:
            decide_release_request(self.staff_user.pk, rel_req.pk, "approve", "Belated approval")
        self.assertEqual(cm.exception.code, "review_conflict")

    def test_stale_ownership_token_rejected(self):
        from .services import assign_or_reassign_account, compute_ownership_token, OfferWorkflowError
        import uuid

        token = compute_ownership_token(self.account)
        assign_or_reassign_account(
            self.staff_user.pk, self.account.pk, self.profile.pk, "First assignment", str(uuid.uuid4()), token
        )

        with self.assertRaises(OfferWorkflowError) as cm:
            assign_or_reassign_account(
                self.staff_user.pk, self.account.pk, self.other_profile.pk, "Second assignment", str(uuid.uuid4()), token
            )
        self.assertEqual(cm.exception.code, "stale_assignment")

    def test_reviewer_client_scope_and_revoked_access_retry(self):
        from .services import assign_or_reassign_account, compute_ownership_token, OfferWorkflowError
        import uuid

        # Reviewer without client access cannot assign
        unscoped_staff = get_user_model().objects.create_user(username="bo-staff-unscoped")
        from django.contrib.auth.models import Permission
        perm = Permission.objects.get(codename="manage_account_assignments", content_type__app_label="assignments")
        unscoped_staff.user_permissions.add(perm)

        token = compute_ownership_token(self.account)
        ev_id = str(uuid.uuid4())
        with self.assertRaises(OfferWorkflowError) as cm:
            assign_or_reassign_account(
                unscoped_staff.pk, self.account.pk, self.profile.pk, "Unscoped assignment", ev_id, token
            )
        self.assertEqual(cm.exception.code, "reviewer_client_access_required")
        self.assertEqual(cm.exception.status, 403)

        # Scoped reviewer assigns successfully
        res = assign_or_reassign_account(
            self.staff_user.pk, self.account.pk, self.profile.pk, "Initial valid assignment", ev_id, token
        )
        self.assertTrue(res["saved"])

        # Reviewer's client access is revoked
        grant = ClientAccess.objects.get(user=self.staff_user, client=self.creditor)
        grant.is_active = False
        grant.save(update_fields=["is_active"])

        # Retry with the same event_id must fail because reviewer access is now revoked
        with self.assertRaises(OfferWorkflowError) as cm:
            assign_or_reassign_account(
                self.staff_user.pk, self.account.pk, self.profile.pk, "Initial valid assignment", ev_id, token
            )
        self.assertEqual(cm.exception.code, "reviewer_client_access_required")
        self.assertEqual(cm.exception.status, 403)

    def test_ongoing_visit_blocks_assignment_and_reassignment(self):
        from .services import assign_or_reassign_account, compute_ownership_token, OfferWorkflowError
        from apps.activities.models import FieldActivity
        import uuid

        token = compute_ownership_token(self.account)
        # Create an in_progress visit on this account
        FieldActivity.objects.create(
            account=self.account,
            status="in_progress",
            kind="visit",
            recorded_by=self.profile.user,
            occurred_at=timezone.now(),
        )

        with self.assertRaises(OfferWorkflowError) as cm:
            assign_or_reassign_account(
                self.staff_user.pk, self.account.pk, self.profile.pk, "Block due to ongoing visit", str(uuid.uuid4()), token
            )
        self.assertEqual(cm.exception.code, "visit_in_progress")
        self.assertEqual(cm.exception.status, 409)

    def test_offer_withdrawal_and_audit_on_assignment(self):
        from .services import assign_or_reassign_account, compute_ownership_token
        from .models import AccountOffer
        from apps.audit.models import AuditEvent
        import uuid

        # Create a pending offer for this account
        offer = AccountOffer.objects.create(
            account=self.account,
            collector=self.profile,
            target_address=self.account.collection_address,
            target_position=self.account.collection_address.position,
            commission_rule=self.account.commission_rule,
            collector_position=self.account.collection_address.position,
            location_captured_at=timezone.now(),
            distance_m=Decimal("100"),
            status="pending",
            expires_at=timezone.now() + timedelta(hours=2),
            matching_snapshot={},
        )

        token = compute_ownership_token(self.account)
        assign_or_reassign_account(
            self.staff_user.pk, self.account.pk, self.other_profile.pk, "Direct assignment withdrawing offers", str(uuid.uuid4()), token
        )

        offer.refresh_from_db()
        self.assertEqual(offer.status, "withdrawn")

        # Verify audit event for offer_withdrawn
        audit_withdrawn = AuditEvent.objects.filter(action="offer_withdrawn", entity_id=str(offer.pk)).first()
        self.assertIsNotNone(audit_withdrawn)
        self.assertEqual(audit_withdrawn.changes["status"]["after"], "withdrawn")

    def test_cannot_assign_to_current_owner_or_self(self):
        from .services import assign_or_reassign_account, compute_ownership_token, OfferWorkflowError
        import uuid

        # Initial assignment to profile
        token0 = compute_ownership_token(self.account)
        assign_or_reassign_account(
            self.staff_user.pk, self.account.pk, self.profile.pk, "Initial assignment", str(uuid.uuid4()), token0
        )

        # Attempt to transfer to the same collector (current owner)
        token1 = compute_ownership_token(self.account)
        with self.assertRaises(OfferWorkflowError) as cm:
            assign_or_reassign_account(
                self.staff_user.pk, self.account.pk, self.profile.pk, "Transfer to same collector", str(uuid.uuid4()), token1
            )
        self.assertEqual(cm.exception.code, "already_assigned_to_collector")
        self.assertEqual(cm.exception.status, 409)

        # Staff user creates a collector profile for themselves and tries to assign to self
        staff_profile = CollectorProfile.objects.create(
            user=self.staff_user,
            approval_status="approved",
            approved_by=self.staff_user,
            approved_at=timezone.now(),
            availability="online",
        )
        with self.assertRaises(OfferWorkflowError) as cm:
            assign_or_reassign_account(
                self.staff_user.pk, self.account.pk, staff_profile.pk, "Assign to self", str(uuid.uuid4()), token1
            )
        self.assertEqual(cm.exception.code, "cannot_assign_self")
        self.assertEqual(cm.exception.status, 403)

    def test_recipient_client_scope_and_backoffice_exclusion(self):
        from .services import assign_or_reassign_account, compute_ownership_token, OfferWorkflowError
        import uuid

        # Collector without client grant
        unscoped_user = get_user_model().objects.create_user(username="collector-unscoped")
        unscoped_prof = CollectorProfile.objects.create(
            user=unscoped_user,
            approval_status="approved",
            approved_by=self.staff_user,
            approved_at=timezone.now(),
            availability="online",
        )

        token = compute_ownership_token(self.account)
        with self.assertRaises(OfferWorkflowError) as cm:
            assign_or_reassign_account(
                self.staff_user.pk, self.account.pk, unscoped_prof.pk, "Assign unscoped collector", str(uuid.uuid4()), token
            )
        self.assertEqual(cm.exception.code, "recipient_client_access_required")
        self.assertEqual(cm.exception.status, 403)

        # Collector who has backoffice access
        from django.contrib.auth.models import Permission
        perm = Permission.objects.get(codename="manage_account_assignments", content_type__app_label="assignments")
        self.other_profile.user.user_permissions.add(perm)
        with self.assertRaises(OfferWorkflowError) as cm:
            assign_or_reassign_account(
                self.staff_user.pk, self.account.pk, self.other_profile.pk, "Assign staff collector", str(uuid.uuid4()), token
            )
        self.assertEqual(cm.exception.code, "ordinary_collector_required")
        self.assertEqual(cm.exception.status, 403)

    def test_long_reason_bounded_in_audit_and_assigned_by_set(self):
        from .services import assign_or_reassign_account, compute_ownership_token
        from apps.audit.models import AuditEvent
        import uuid

        token = compute_ownership_token(self.account)
        long_reason = "Operational justification: " + ("ABCDE" * 80) # 428 characters
        ev_id = str(uuid.uuid4())

        res = assign_or_reassign_account(
            self.staff_user.pk, self.account.pk, self.profile.pk, long_reason, ev_id, token
        )
        self.assertTrue(res["saved"])
        assignment = res["assignment"]

        # assigned_by FK is populated
        self.assertEqual(assignment.assigned_by_id, self.staff_user.pk)
        # Snapshot holds full text
        self.assertEqual(assignment.policy_snapshot["assignment_reason"], long_reason)

        # AuditEvent change for reason is bounded to 100 characters
        audit_event = AuditEvent.objects.filter(action="account_assigned", entity_id=str(self.account.pk)).first()
        self.assertIsNotNone(audit_event)
        self.assertLessEqual(len(audit_event.changes["reason"]), 100)
        self.assertEqual(audit_event.changes["reason"], long_reason[:100])

    def test_invalid_account_configuration_rejected_safely(self):
        from .services import assign_or_reassign_account, compute_ownership_token, OfferWorkflowError
        import uuid

        token = compute_ownership_token(self.account)

        # Missing collection address
        self.account.collection_address = None
        self.account.save(update_fields=["collection_address"])
        with self.assertRaises(OfferWorkflowError) as cm:
            assign_or_reassign_account(
                self.staff_user.pk, self.account.pk, self.profile.pk, "Missing address test", str(uuid.uuid4()), token
            )
        self.assertEqual(cm.exception.code, "unverified_address")

        # Restore address, break commission rule
        self.account.collection_address = self.creditor.customers.first().addresses.first()
        self.account.commission_rule = None
        self.account.save(update_fields=["collection_address", "commission_rule"])
        with self.assertRaises(OfferWorkflowError) as cm:
            assign_or_reassign_account(
                self.staff_user.pk, self.account.pk, self.profile.pk, "Missing rule test", str(uuid.uuid4()), token
            )
        self.assertEqual(cm.exception.code, "inactive_commission_rule")


class AccountAssignmentConcurrencyTests(TransactionTestCase):
    """
    Concurrency verification for competing Back Office reassignment operations.
    Requires a multi-connection test database.
    """
    def test_competing_reassignments_preserve_one_active_owner(self):
        from .services import assign_or_reassign_account, compute_ownership_token, OfferWorkflowError
        from apps.user.models import ClientAccess
        from django.contrib.auth.models import Permission
        import uuid

        user, profile, creditor, account = offer_fixture(second=True)
        other_profile = CollectorProfile.objects.exclude(pk=profile.pk).first()

        # Create a third collector
        third_user = get_user_model().objects.create_user(username="collector-concurrency-3")
        third_profile = CollectorProfile.objects.create(
            user=third_user, approval_status="approved", approved_by=user,
            approved_at=timezone.now(), availability="online"
        )
        ClientAccess.objects.create(user=third_user, client=creditor, is_active=True)

        perm = Permission.objects.get(codename="manage_account_assignments", content_type__app_label="assignments")

        rev1 = get_user_model().objects.create_user(username="staff-reassign-1")
        rev1.user_permissions.add(perm)
        ClientAccess.objects.create(user=rev1, client=creditor, is_active=True)

        rev2 = get_user_model().objects.create_user(username="staff-reassign-2")
        rev2.user_permissions.add(perm)
        ClientAccess.objects.create(user=rev2, client=creditor, is_active=True)

        # Initial assignment
        token0 = compute_ownership_token(account)
        assign_or_reassign_account(
            rev1.pk, account.pk, profile.pk, "Initial assignment", str(uuid.uuid4()), token0
        )

        token1 = compute_ownership_token(account)
        barrier = Barrier(2, timeout=10)

        def reassign(reviewer_id, recipient_id, note):
            close_old_connections()
            try:
                barrier.wait()
                try:
                    return assign_or_reassign_account(
                        reviewer_id, account.pk, recipient_id, note, str(uuid.uuid4()), token1
                    )
                except OfferWorkflowError as exc:
                    return {"error": exc.code}
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(reassign, rev1.pk, other_profile.pk, "Reassign by rev1")
            f2 = pool.submit(reassign, rev2.pk, third_profile.pk, "Reassign by rev2")
            results = [f1.result(timeout=25), f2.result(timeout=25)]

        successes = [r for r in results if r.get("saved") is True and r.get("created") is True]
        conflicts = [r for r in results if r.get("error") in {"stale_assignment", "busy", "event_conflict"}]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(conflicts), 1)

        # Exactly one active assignment exists on the account
        self.assertEqual(AccountAssignment.objects.filter(account=account, status="active").count(), 1)


class AccountDetailContactAttemptTests(TestCase):
    def setUp(self):
        self.user, self.profile, self.creditor, self.account = offer_fixture()
        offer_id = refresh_offers(self.user.pk)["offers"][0]["id"]
        respond_to_offer(self.user.pk, offer_id, "accept")
        self.assignment = AccountAssignment.objects.get(account=self.account, status="active")
        self.url = reverse("assignments:account_detail", args=[self.assignment.pk])
        self.client.force_login(self.user)

    def test_account_detail_provides_contact_attempt_context_and_url(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        expected_url = reverse("activities:record_contact_attempt", args=[self.assignment.pk])
        self.assertEqual(response.context["contact_attempt_url"], expected_url)
        self.assertEqual(
            response.context["contact_attempt_methods"],
            [("phone", "Call"), ("sms", "SMS"), ("email", "Email")],
        )
        self.assertEqual(
            response.context["contact_attempt_outcomes"],
            [
                ("contacted", "Customer contacted"),
                ("unavailable", "Customer unavailable"),
                ("disputed", "Customer disputed the account"),
                ("other", "Other"),
            ],
        )
        self.assertIn("contact_attempt_history", response.context)

    def test_scoped_history_and_separation_from_visits(self):
        from apps.activities.models import FieldActivity
        now = timezone.now()

        # Create a contact attempt
        contact_attempt = FieldActivity.objects.create(
            account=self.account,
            assignment=self.assignment,
            recorded_by=self.user,
            event_id=uuid4(),
            kind="phone",
            status="submitted",
            review_status="not_required",
            outcome="contacted",
            notes="Phone call attempt details.",
            occurred_at=now,
            ended_at=now,
            submission_fingerprint="fp1",
        )

        # Create a visit
        visit = FieldActivity.objects.create(
            account=self.account,
            assignment=self.assignment,
            recorded_by=self.user,
            event_id=uuid4(),
            kind="visit",
            status="submitted",
            review_status="not_required",
            outcome="contacted",
            notes="Physical field visit details.",
            occurred_at=now - timedelta(hours=1),
            ended_at=now - timedelta(hours=1),
            submission_fingerprint="fp2",
            location_issue_reason="No GPS signal.",
        )

        # Another assignment's contact attempt (should not leak)
        other_user, other_profile, _, other_account = offer_fixture(username="other_u", email="other_u@example.com")
        from apps.assignments.models import AccountAssignment
        other_assignment = AccountAssignment.objects.create(
            account=other_account,
            collector=other_profile,
            status="active",
            target_address=other_account.collection_address,
            target_position=other_account.collection_address.position,
            commission_terms="10%",
            first_action_due_at=now + timedelta(days=1),
        )
        other_attempt = FieldActivity.objects.create(
            account=other_account,
            assignment=other_assignment,
            recorded_by=other_user,
            event_id=uuid4(),
            kind="sms",
            status="submitted",
            review_status="not_required",
            outcome="unavailable",
            notes="Other collector's attempt.",
            occurred_at=now,
            ended_at=now,
            submission_fingerprint="fp3",
        )

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

        # Contact attempt history must only contain this assignment's contact attempts
        attempt_history = list(response.context["contact_attempt_history"])
        self.assertIn(contact_attempt, attempt_history)
        self.assertNotIn(visit, attempt_history)
        self.assertNotIn(other_attempt, attempt_history)

        # Visit history must only contain visits, never contact attempts
        visit_history = list(response.context["visit_history"])
        self.assertIn(visit, visit_history)
        self.assertNotIn(contact_attempt, visit_history)
        self.assertNotIn(other_attempt, visit_history)

        # HTML layout check: Contact attempt section placed directly above Collection visit
        content = response.content.decode("utf-8")
        contact_pos = content.find("id=\"contact-attempt-heading\"")
        visit_pos = content.find("id=\"visit-heading\"")
        self.assertNotEqual(contact_pos, -1)
        self.assertNotEqual(visit_pos, -1)
        self.assertLess(contact_pos, visit_pos, "Contact attempt section must appear before Collection visit section.")

        # HTML content check: contact attempt details rendered
        self.assertIn("Call", content)
        self.assertIn("Phone call attempt details.", content)


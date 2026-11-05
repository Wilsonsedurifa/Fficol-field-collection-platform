import json
from importlib import import_module
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone as utc_timezone
from decimal import Decimal
from threading import Barrier
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from unittest.mock import patch
from uuid import uuid4
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Permission
from django.core.paginator import Paginator
from django.db import IntegrityError, close_old_connections, connections, transaction
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from apps.assignments.models import AccountAssignment
from apps.assignments.services import OfferWorkflowError, refresh_offers, respond_to_offer
from apps.assignments.tests import offer_fixture
from apps.audit.models import AuditEvent
from apps.collection.models import PromiseToPay
from apps.user.models import ClientAccess, CollectorProfile
from .models import FieldActivity, FollowUpTask
from .services import validate_start, validate_completion, _fingerprint, start_visit, submit_visit, cancel_visit, schedule_revisit, visit_gate, followup_day_end, followup_period, can_review_visits, validate_review, reviewable_visits, review_visit, can_schedule_followups, validate_schedule_window, task_window_state, decide_followup, validate_contact_attempt, record_contact_attempt
from . import views


def start_payload(**values):
    return {"event_id": str(uuid4()), "location": None, "location_issue_reason": "Phone could not acquire location.", **values}


def completion(**values):
    return {"outcome": "contacted", "notes": "Customer contacted at collection address.", "promise": None, "follow_up": None, **values}


class VisitValidationTests(SimpleTestCase):
    def test_missing_gps_requires_explanation(self):
        self.assertIsNone(validate_start(start_payload())[1])
        with self.assertRaises(OfferWorkflowError):
            validate_start(start_payload(location_issue_reason=" "))

    def test_location_values_are_bounded_and_aware(self):
        observation={"longitude":120.9842,"latitude":14.5995,"accuracy_m":Decimal("15.001"),"captured_at":timezone.now().isoformat()}
        _,evidence,_=validate_start(start_payload(location=observation,location_issue_reason=""))
        self.assertEqual(evidence['accuracy_m'],Decimal('15.01'))
        for key,value in [('latitude',True),('longitude',181),('latitude',float('nan')),('accuracy_m',-1),('accuracy_m',1000000),('captured_at','2026-10-13T10:00:00')]:
            with self.subTest(key=key,value=value),self.assertRaises(OfferWorkflowError):
                validate_start(start_payload(location={**observation,key:value}))

    def test_uuid_and_unknown_start_fields_rejected(self):
        for payload in [start_payload(event_id='bad'),start_payload(account_id=999)]:
            with self.assertRaises(OfferWorkflowError):validate_start(payload)

    def test_controlled_outcomes_and_required_notes(self):
        now=timezone.now()
        for payload in [completion(outcome='payment_reported'),completion(outcome=[]),completion(outcome='wrong_address',notes=''),completion(outcome='disputed',notes=''),completion(notes='x'*5001)]:
            with self.assertRaises(OfferWorkflowError):validate_completion(payload,now)

    def test_promises_require_exact_money_and_calendar_date(self):
        now=timezone.now();future=(now+timedelta(days=2)).date().isoformat()
        value=validate_completion(completion(outcome='promise_to_pay',promise={'amount':'250.00','due_date':future}),now)
        self.assertEqual(value['promise']['amount'],'250.00')
        for amount in ['0','-1','NaN','Infinity','1.001','1000000000000',250]:
            with self.subTest(amount=amount),self.assertRaises(OfferWorkflowError):
                validate_completion(completion(outcome='promise_to_pay',promise={'amount':amount,'due_date':future}),now)

    def test_unrelated_promise_and_past_date_rejected(self):
        now=timezone.now()
        for payload in [completion(promise={'amount':'1','due_date':'2000-01-01'}),completion(outcome='promise_to_pay',promise={'amount':'1','due_date':'2000-01-01'})]:
            with self.assertRaises(OfferWorkflowError):validate_completion(payload,now)

    def test_followups_require_valid_future_time(self):
        now=timezone.now()
        good={'kind':'revisit','due_at':(now+timedelta(days=1)).isoformat()}
        self.assertIsNotNone(validate_completion(completion(follow_up=good),now)['follow_up'])
        for follow in [{'kind':[],'due_at':good['due_at']},{'kind':'review','due_at':good['due_at']},{'kind':'promise_check','due_at':good['due_at']},{'kind':'call','due_at':now.isoformat()},{'kind':'call','due_at':'not-a-date'}]:
            with self.assertRaises(OfferWorkflowError):validate_completion(completion(follow_up=follow),now)

    def test_exact_retry_normalizes_money_and_timezone(self):
        now=timezone.now()
        first=completion(outcome='promise_to_pay',promise={'amount':'10','due_date':'2000-01-01'},follow_up={'kind':'promise_check','due_at':'2000-01-01T08:00:00+08:00'})
        second=completion(outcome='promise_to_pay',promise={'amount':'10.00','due_date':'2000-01-01'},follow_up={'kind':'promise_check','due_at':'2000-01-01T00:00:00Z'})
        self.assertEqual(_fingerprint(validate_completion(first,now,False)),_fingerprint(validate_completion(second,now,False)))


class VisitEndpointTests(SimpleTestCase):
    def setUp(self):self.factory=RequestFactory()

    def request(self,body='{}',method='post',user=None):
        request=self.factory.generic(method.upper(),'/collector/accounts/1/visits/start/',data=body,content_type='application/json')
        request.user=user or SimpleNamespace(is_authenticated=True,is_active=True,pk=1)
        request._dont_enforce_csrf_checks=True
        return request

    def test_authentication_and_method(self):
        self.assertEqual(views.start(self.request(user=AnonymousUser()),assignment_id=1).status_code,401)
        self.assertEqual(views.start(self.request(method='get'),assignment_id=1).status_code,405)

    def test_duplicate_nonfinite_and_oversize_json_rejected(self):
        for body in ['{"event_id":"a","event_id":"b"}','{"a":NaN}','x'*32769]:
            self.assertEqual(views.start(self.request(body),assignment_id=1).status_code,400)

    def test_csrf_is_enforced(self):
        response=Client(enforce_csrf_checks=True).post(reverse('activities:start_visit',args=[1]),data='{}',content_type='application/json')
        self.assertEqual(response.status_code,403)

    def test_service_errors_are_controlled(self):
        self.assertEqual(views.start(self.request(),assignment_id=1).status_code,400)


class VisitPersistenceTests(TestCase):
    def setUp(self):
        self.user,self.profile,self.creditor,self.account=offer_fixture()
        offer_id=refresh_offers(self.user.pk)['offers'][0]['id']
        respond_to_offer(self.user.pk,offer_id,'accept')
        self.assignment=AccountAssignment.objects.get(account=self.account,status='active')

    def begin(self,**values):return start_visit(self.user.pk,self.assignment.pk,start_payload(**values))['visit']['id']

    def test_start_retry_and_different_request_resume_one_visit(self):
        payload=start_payload()
        first=start_visit(self.user.pk,self.assignment.pk,payload)
        retry=start_visit(self.user.pk,self.assignment.pk,payload)
        resume=start_visit(self.user.pk,self.assignment.pk,start_payload())
        self.assertEqual(first['visit'],retry['visit']);self.assertEqual(first['visit'],resume['visit'])
        self.assertEqual(FieldActivity.objects.count(),1)
        self.assertEqual(AuditEvent.objects.filter(action='visit_started').count(),1)

    def test_submit_with_promise_followup_and_exact_retry(self):
        visit_id=self.begin();now=timezone.now()
        payload=completion(outcome='promise_to_pay',promise={'amount':'500.00','due_date':(now+timedelta(days=2)).date().isoformat()},follow_up={'kind':'revisit','due_at':(now+timedelta(days=2)).isoformat()})
        first=submit_visit(self.user.pk,self.assignment.pk,visit_id,payload)
        with patch('apps.activities.services.timezone.now',return_value=now+timedelta(days=4)):
            retry=submit_visit(self.user.pk,self.assignment.pk,visit_id,payload)
        self.assertEqual(first['visit'],retry['visit']);self.assertFalse(retry['changed'])
        self.assertEqual(PromiseToPay.objects.count(),1);self.assertEqual(FollowUpTask.objects.count(),1)
        self.assertEqual(AuditEvent.objects.filter(action='visit_submitted').count(),1)
        self.assignment.refresh_from_db();self.account.refresh_from_db()
        self.assertEqual(self.assignment.status,'active');self.assertEqual(self.account.balance_snapshot,Decimal('1000'))
        self.assertIsNone(self.assignment.next_action_due_at)
        self.assertEqual(FollowUpTask.objects.get().status,'requested')
        self.assertEqual(FieldActivity.objects.get().review_status,'pending')
        with self.assertRaises(OfferWorkflowError):submit_visit(self.user.pk,self.assignment.pk,visit_id,completion())

    def test_transaction_rolls_back_report_and_related_records(self):
        visit_id=self.begin();now=timezone.now()
        payload=completion(outcome='promise_to_pay',promise={'amount':'10','due_date':(now+timedelta(days=1)).date().isoformat()},follow_up={'kind':'revisit','due_at':(now+timedelta(days=1)).isoformat()})
        with patch('apps.activities.services.append_audit_event',side_effect=RuntimeError('test failure')),self.assertRaises(RuntimeError):
            submit_visit(self.user.pk,self.assignment.pk,visit_id,payload)
        self.assertEqual(FieldActivity.objects.get().status,'in_progress')
        self.assertFalse(PromiseToPay.objects.exists());self.assertFalse(FollowUpTask.objects.exists())

    def test_cancel_is_retained_and_repeat_safe(self):
        visit_id=self.begin();payload={'reason':'Customer requested a different day.'}
        first=cancel_visit(self.user.pk,self.assignment.pk,visit_id,payload)
        self.assertEqual(first['visit'],cancel_visit(self.user.pk,self.assignment.pk,visit_id,payload)['visit'])
        self.assertEqual(FieldActivity.objects.get().status,'cancelled')
        with self.assertRaises(OfferWorkflowError):submit_visit(self.user.pk,self.assignment.pk,visit_id,completion())
        self.begin();self.assertEqual(FieldActivity.objects.count(),2)

    def test_revocation_and_ownership_changes_block_mutations(self):
        visit_id=self.begin()
        ClientAccess.objects.filter(user=self.user).update(is_active=False)
        with self.assertRaises(OfferWorkflowError):submit_visit(self.user.pk,self.assignment.pk,visit_id,completion())
        ClientAccess.objects.filter(user=self.user).update(is_active=True)
        AccountAssignment.objects.filter(pk=self.assignment.pk).update(status='released',ended_at=timezone.now())
        with self.assertRaises(OfferWorkflowError):cancel_visit(self.user.pk,self.assignment.pk,visit_id,{'reason':'No access'})
        self.assertEqual(FieldActivity.objects.get().status,'in_progress')

    def test_offline_visits_work_but_inactive_or_unapproved_collectors_are_denied(self):
        CollectorProfile.objects.filter(pk=self.profile.pk).update(availability='offline')
        visit_id=self.begin()
        get_user_model().objects.filter(pk=self.user.pk).update(is_active=False)
        with self.assertRaises(OfferWorkflowError):submit_visit(self.user.pk,self.assignment.pk,visit_id,completion())
        get_user_model().objects.filter(pk=self.user.pk).update(is_active=True)
        CollectorProfile.objects.filter(pk=self.profile.pk).update(approval_status='pending')
        with self.assertRaises(OfferWorkflowError):cancel_visit(self.user.pk,self.assignment.pk,visit_id,{'reason':'Changed access'})

    def test_other_collector_cannot_read_or_change_visit(self):
        visit_id=self.begin()
        other=get_user_model().objects.create_user(username='visit-other')
        CollectorProfile.objects.create(user=other,approval_status='approved',approved_by=self.user,approved_at=timezone.now())
        ClientAccess.objects.create(user=other,client=self.creditor)
        with self.assertRaises(OfferWorkflowError):submit_visit(other.pk,self.assignment.pk,visit_id,completion())
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse('assignments:account_detail',args=[self.assignment.pk])).status_code,404)

    def test_gps_quality_issue_needs_reason_and_keeps_evidence(self):
        observation={'latitude':14.5995,'longitude':120.9842,'accuracy_m':500,'captured_at':timezone.now().isoformat()}
        with self.assertRaises(OfferWorkflowError):self.begin(location=observation,location_issue_reason='')
        visit_id=self.begin(location=observation,location_issue_reason='Poor signal at the collection address.')
        activity=FieldActivity.objects.get(pk=visit_id)
        self.assertIsNotNone(activity.distance_m);self.assertEqual(activity.accuracy_m,Decimal('500'))

    def test_ongoing_constraint_and_history_rendering(self):
        visit_id=self.begin();activity=FieldActivity.objects.get(pk=visit_id)
        with self.assertRaises(IntegrityError),transaction.atomic():
            FieldActivity.objects.create(account=self.account,assignment=self.assignment,recorded_by=self.user,occurred_at=timezone.now(),location_issue_reason='No GPS')
        self.client.force_login(self.user)
        response=self.client.get(reverse('assignments:account_detail',args=[self.assignment.pk]))
        self.assertEqual(response.context['visit_config']['ongoing']['id'],visit_id)
        submit_visit(self.user.pk,self.assignment.pk,visit_id,completion(notes='<script>bad()</script>'))
        response=self.client.get(reverse('assignments:account_detail',args=[self.assignment.pk]))
        self.assertNotContains(response,'<script>bad()</script>');self.assertContains(response,'Pending review')

    def test_review_migration_preserves_real_review_evidence(self):
        from django.apps import apps
        now=timezone.now()
        def report(**values):
            return FieldActivity.objects.create(account=self.account,assignment=self.assignment,recorded_by=self.user,status='submitted',outcome='contacted',occurred_at=now,ended_at=now,location_issue_reason='No location available',submission_fingerprint='a'*64,review_status='pending',**values)
        ordinary=report()
        flagged=report(review_reason='Needs investigation')
        approved=report()
        FieldActivity.objects.filter(pk=approved.pk).update(review_status='approved',reviewed_by=self.user,reviewed_at=now)
        migration=import_module('apps.activities.migrations.0002_remove_fieldactivity_activity_valid_lifecycle_and_more')
        migration.remove_automatic_visit_review(apps,SimpleNamespace(connection=SimpleNamespace(alias='default')))
        ordinary.refresh_from_db();flagged.refresh_from_db();approved.refresh_from_db()
        self.assertEqual(ordinary.review_status,'not_required')
        self.assertEqual(flagged.review_status,'pending')
        self.assertEqual(approved.review_status,'approved')
        self.assertEqual(approved.reviewed_at,now)


class VisitConcurrencyTests(TransactionTestCase):
    def test_simultaneous_start_creates_one_ongoing_visit(self):
        user,profile,creditor,account=offer_fixture()
        offer_id=refresh_offers(user.pk)['offers'][0]['id'];respond_to_offer(user.pk,offer_id,'accept')
        assignment=AccountAssignment.objects.get(account=account,status='active')
        barrier=Barrier(2,timeout=10)
        def begin():
            close_old_connections()
            try:
                barrier.wait();return start_visit(user.pk,assignment.pk,start_payload())['visit']['id']
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(begin) for _ in range(2)]
            results=[future.result(timeout=20) for future in futures]
        self.assertEqual(results[0],results[1]);self.assertEqual(FieldActivity.objects.count(),1)

    def test_two_repeat_requests_reserve_one_scheduled_revisit(self):
        user,profile,creditor,account=offer_fixture()
        offer_id=refresh_offers(user.pk)['offers'][0]['id'];respond_to_offer(user.pk,offer_id,'accept')
        assignment=AccountAssignment.objects.get(account=account,status='active')
        original=start_visit(user.pk,assignment.pk,start_payload())['visit']['id']
        submit_visit(user.pk,assignment.pk,original,completion())
        due=timezone.now()+timedelta(minutes=1)
        task={'id':FollowUpTask.objects.create(account=account,assignment=assignment,source_activity_id=original,assignee=user,kind='revisit',status='pending',due_at=due).pk}
        barrier=Barrier(2,timeout=10)
        def begin():
            close_old_connections()
            try:
                barrier.wait();return start_visit(user.pk,assignment.pk,start_payload())['visit']['id']
            finally:connections.close_all()
        with patch('apps.activities.services.timezone.now',return_value=due),ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(begin) for _ in range(2)]
            results=[future.result(timeout=20) for future in futures]
        self.assertEqual(results[0],results[1])
        self.assertEqual(FieldActivity.objects.filter(scheduled_followup_id=task['id'],status='in_progress').count(),1)


class FollowupTimeTests(SimpleTestCase):
    def test_categories_are_complementary_at_philippine_midnight(self):
        now=datetime(2026,10,12,16,30,tzinfo=utc_timezone.utc)
        end=followup_day_end(now)
        self.assertEqual(end,datetime(2026,10,13,16,tzinfo=utc_timezone.utc))
        for due,expected in [(now-timedelta(seconds=1),'overdue'),(now,'today'),(end-timedelta(seconds=1),'today'),(end,'upcoming')]:
            self.assertEqual(followup_period(SimpleNamespace(due_at=due),now),expected)

    def test_anonymous_page_redirects_and_post_is_rejected(self):
        url=reverse('activities:followups')
        self.assertEqual(self.client.get(url).status_code,302)
        request=RequestFactory().post(url)
        request.user=SimpleNamespace(is_authenticated=True,is_active=True,pk=1)
        self.assertEqual(views.followups_page(request).status_code,405)


class OptionalVisitReviewTests(SimpleTestCase):
    def test_draft_default_retains_legacy_state_but_submission_policy_is_separate(self):
        self.assertEqual(FieldActivity().review_status,'not_required')

    def test_migration_preserves_completed_flagged_and_nonvisit_reviews(self):
        migration=import_module('apps.activities.migrations.0002_remove_fieldactivity_activity_valid_lifecycle_and_more')
        rows=[dict(kind='visit',status='submitted',review_status='pending',reviewed_by=None,reviewed_at=None,review_reason='')]
        for values in [dict(review_status='approved',reviewed_by=1,reviewed_at='review time'),dict(review_status='rejected',reviewed_by=1,reviewed_at='review time',review_reason='Invalid evidence'),dict(review_reason='Needs investigation'),dict(kind='phone'),dict(status='in_progress'),dict(status='cancelled')]:
            rows.append({**rows[0],**values})
        expected=[dict(row) for row in rows]
        class Records:
            def __init__(self,items):self.items=items
            def using(self,alias):
                self.alias=alias
                return self
            def filter(self,**conditions):
                def matches(row):
                    return all((row[key[:-8]] is None)==value if key.endswith('__isnull') else row[key]==value for key,value in conditions.items())
                return Records([row for row in self.items if matches(row)])
            def update(self,**values):
                for row in self.items:row.update(values)
        records=Records(rows)
        registry=SimpleNamespace(get_model=lambda app,name:SimpleNamespace(objects=records))
        editor=SimpleNamespace(connection=SimpleNamespace(alias='test-alias'))
        migration.remove_automatic_visit_review(registry,editor)
        self.assertEqual(records.alias,'test-alias')
        self.assertEqual(rows[0]['review_status'],'not_required')
        self.assertEqual(rows[1:],expected[1:])
        migration.restore_pending_state(registry,editor)
        self.assertEqual(rows,expected)


class RevisitGateTests(SimpleTestCase):
    def test_first_visit_and_scheduled_waiting_boundaries(self):
        now=timezone.now();assignment=SimpleNamespace(pk=1,account_id=1)
        with patch('apps.activities.services.FieldActivity.objects') as records,patch('apps.activities.services.FollowUpTask.objects') as tasks:
            records.filter.return_value.order_by.return_value.first.return_value=None
            records.filter.return_value.exclude.return_value.exists.return_value=False
            rows=tasks.filter.return_value.exclude.return_value.filter.return_value
            rows.order_by.return_value.first.return_value=None
            tasks.filter.return_value.exists.return_value=False
            self.assertEqual(visit_gate(assignment,1,now)['state'],'first_visit')
            records.filter.return_value.order_by.return_value.first.return_value=SimpleNamespace(pk=2)
            self.assertEqual(visit_gate(assignment,1,now)['state'],'schedule_required')
            tasks.filter.return_value.exists.return_value=True
            self.assertEqual(visit_gate(assignment,1,now)['state'],'requested')
            task=SimpleNamespace(pk=3,due_at=now+timedelta(hours=1),available_from=now+timedelta(seconds=1))
            rows.order_by.return_value.first.return_value=task
            self.assertEqual(visit_gate(assignment,1,now)['state'],'waiting')
            task.available_from=now
            self.assertTrue(visit_gate(assignment,1,now)['allowed'])
            self.assertEqual(visit_gate(assignment,1,task.due_at+timedelta(seconds=1))['state'],'overdue')

    def test_schedule_endpoint_rejects_missing_date_and_checks_csrf(self):
        request=RequestFactory().post('/schedule/',data='{}',content_type='application/json')
        request.user=SimpleNamespace(is_authenticated=True,is_active=True,pk=1);request._dont_enforce_csrf_checks=True
        self.assertEqual(views.schedule(request,assignment_id=1,activity_id=1).status_code,400)
        self.assertEqual(Client(enforce_csrf_checks=True).post(reverse('activities:schedule_revisit',args=[1,1]),data='{}',content_type='application/json').status_code,403)


class BackOfficeValidationTests(SimpleTestCase):
    def test_staff_status_does_not_replace_review_permission(self):
        for active,permission,expected in [(True,False,False),(False,True,False),(True,True,True)]:
            user=SimpleNamespace(is_authenticated=True,is_active=active,is_staff=True,has_perm=lambda name:permission)
            self.assertEqual(can_review_visits(user),expected)
        self.assertFalse(can_review_visits(AnonymousUser()))

    def test_rejection_requires_bounded_reason_and_controlled_decision(self):
        self.assertEqual(validate_review('approve','  Checked notes  '),'Checked notes')
        self.assertEqual(validate_review('approve',''),'')
        for decision,reason in [('reject',' '),('approve','x'*5001),('approved',''),([],''),('reject',None)]:
            with self.assertRaises(OfferWorkflowError):validate_review(decision,reason)

    def test_unauthorized_queue_and_csrf_requests_are_rejected(self):
        request=RequestFactory().get('/back-office/');request.user=SimpleNamespace(is_authenticated=True,is_active=True,has_perm=lambda name:False)
        from django.core.exceptions import PermissionDenied
        with self.assertRaises(PermissionDenied):views.review_dashboard(request)
        self.assertEqual(self.client.get(reverse('supervisor_dashboard')).status_code,302)
        self.assertEqual(Client(enforce_csrf_checks=True).post(reverse('review_visit',args=[1]),{'decision':'approve','reason':''}).status_code,403)

    def test_native_form_post_redirects_only_after_confirmed_review(self):
        from django.contrib.messages.storage.fallback import FallbackStorage
        page=Paginator([],10).get_page(1)
        context={'reports':page,'page_obj':page,'review_counts':{'pending':0,'approved':0,'rejected':0,'legacy':0},'review_state':'pending','search_query':''}
        request=RequestFactory().post('/back-office/visits/1/review/',urlencode({'csrfmiddlewaretoken':'test','decision':'approve','reason':'Checked'}),content_type='application/x-www-form-urlencoded')
        request.user=SimpleNamespace(is_authenticated=True,pk=1,get_full_name='Reviewer',username='Reviewer')
        request._dont_enforce_csrf_checks=True;request.session={};request._messages=FallbackStorage(request)
        with patch('apps.activities.views._review_context',return_value=context),patch('apps.activities.views.review_visit',return_value={'saved':True,'changed':True,'review_status':'approved'}) as service:
            response=views.review_visit_form(request,activity_id=7)
        self.assertEqual(response.status_code,302);self.assertEqual(response.url,reverse('backoffice_visits'))
        service.assert_called_once_with(1,7,'approve','Checked')


class BackOfficeConcurrencyTests(TransactionTestCase):
    def test_competing_reviewers_cannot_overwrite_each_other(self):
        user,profile,creditor,account=offer_fixture()
        offer_id=refresh_offers(user.pk)['offers'][0]['id'];respond_to_offer(user.pk,offer_id,'accept')
        assignment=AccountAssignment.objects.get(account=account,status='active')
        visit_id=start_visit(user.pk,assignment.pk,start_payload())['visit']['id']
        submit_visit(user.pk,assignment.pk,visit_id,completion())
        permission=Permission.objects.get(codename='change_fieldactivity',content_type__app_label='activities')
        reviewers=[]
        for index in range(2):
            reviewer=get_user_model().objects.create_user(username=f'reviewer-race-{index}')
            reviewer.user_permissions.add(permission);ClientAccess.objects.create(user=reviewer,client=creditor)
            reviewers.append(reviewer.pk)
        barrier=Barrier(2,timeout=10)
        def decide(pair):
            close_old_connections()
            try:
                barrier.wait()
                try:return review_visit(pair[0],visit_id,pair[1],'Checked')
                except OfferWorkflowError as exc:return {'error':exc.code}
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(decide,pair) for pair in zip(reviewers,['approve','reject'])]
            results=[future.result(timeout=20) for future in futures]
        self.assertEqual(sum(result.get('saved') is True for result in results),1)
        self.assertEqual(AuditEvent.objects.filter(action='visit_reviewed').count(),1)


class BackOfficeReviewTests(TestCase):
    setUpFixture=VisitPersistenceTests.setUp
    begin=VisitPersistenceTests.begin

    def setUp(self):
        self.setUpFixture()
        self.visit_id=self.begin();submit_visit(self.user.pk,self.assignment.pk,self.visit_id,completion())
        self.reviewer=get_user_model().objects.create_user(username='back-office-reviewer')
        self.permission=Permission.objects.get(codename='change_fieldactivity',content_type__app_label='activities')
        self.reviewer.user_permissions.add(self.permission)
        ClientAccess.objects.create(user=self.reviewer,client=self.creditor)

    def test_approval_is_audited_repeat_safe_and_financially_independent(self):
        gate_before=visit_gate(self.assignment,self.user.pk,timezone.now())
        first=review_visit(self.reviewer.pk,self.visit_id,'approve','Evidence checked')
        activity=FieldActivity.objects.get(pk=self.visit_id);reviewed_at=activity.reviewed_at
        retry=review_visit(self.reviewer.pk,self.visit_id,'approve','Evidence checked')
        self.assertTrue(first['changed']);self.assertFalse(retry['changed'])
        activity.refresh_from_db();self.assertEqual(activity.reviewed_at,reviewed_at)
        self.assertEqual(activity.reviewed_by_id,self.reviewer.pk)
        self.assertEqual(activity.review_status,'approved')
        self.assertEqual(AuditEvent.objects.filter(action='visit_reviewed').count(),1)
        self.assertEqual(AuditEvent.objects.get(action='visit_reviewed').changes,{'review_status':{'before':'pending','after':'approved'}})
        self.account.refresh_from_db();self.assignment.refresh_from_db()
        self.assertEqual(self.account.balance_snapshot,Decimal('1000'));self.assertEqual(self.assignment.status,'active')
        self.assertEqual(visit_gate(self.assignment,self.user.pk,timezone.now()),gate_before)
        self.assertFalse(PromiseToPay.objects.exists())

    def test_rejection_requires_reason_and_decision_is_immutable(self):
        with self.assertRaises(OfferWorkflowError):review_visit(self.reviewer.pk,self.visit_id,'reject','')
        self.assertEqual(FieldActivity.objects.get(pk=self.visit_id).review_status,'pending')
        review_visit(self.reviewer.pk,self.visit_id,'reject','No supporting evidence')
        with self.assertRaises(OfferWorkflowError) as caught:review_visit(self.reviewer.pk,self.visit_id,'approve','')
        self.assertEqual(caught.exception.code,'review_conflict')
        self.client.force_login(self.user)
        response=self.client.get(reverse('assignments:account_detail',args=[self.assignment.pk]))
        self.assertContains(response,'Rejected review');self.assertContains(response,'No supporting evidence')

    def test_permission_and_client_scope_cannot_be_bypassed(self):
        ClientAccess.objects.filter(user=self.reviewer).update(is_active=False)
        with self.assertRaises(OfferWorkflowError) as caught:review_visit(self.reviewer.pk,self.visit_id,'approve','')
        self.assertEqual(caught.exception.status,404)
        self.assertFalse(reviewable_visits(self.reviewer).exists())
        ClientAccess.objects.filter(user=self.reviewer).update(is_active=True)
        self.reviewer.user_permissions.clear()
        with self.assertRaises(OfferWorkflowError) as caught:review_visit(self.reviewer.pk,self.visit_id,'approve','')
        self.assertEqual(caught.exception.status,403)

    def test_collector_cannot_review_own_report_even_with_permission(self):
        self.user.user_permissions.add(self.permission)
        with self.assertRaises(OfferWorkflowError) as caught:review_visit(self.user.pk,self.visit_id,'approve','')
        self.assertEqual(caught.exception.code,'self_review')
        self.assertFalse(reviewable_visits(get_user_model().objects.get(pk=self.user.pk)).exists())

    def test_legacy_reports_are_visible_but_not_silently_enrolled(self):
        FieldActivity.objects.filter(pk=self.visit_id).update(review_status='not_required')
        self.client.force_login(self.reviewer)
        self.assertEqual(self.client.get(reverse('backoffice_visits')).context['page_obj'].paginator.count,0)
        response=self.client.get(reverse('backoffice_visits'),{'state':'legacy'})
        self.assertContains(response,'Earlier report');self.assertNotContains(response,'Approve report')
        with self.assertRaises(OfferWorkflowError) as caught:review_visit(self.reviewer.pk,self.visit_id,'approve','')
        self.assertEqual(caught.exception.code,'legacy_review')

    def test_form_submission_login_routing_and_escaped_evidence(self):
        FieldActivity.objects.filter(pk=self.visit_id).update(notes='<script>unsafe()</script>')
        self.client.force_login(self.reviewer)
        response=self.client.get(reverse('login_success'))
        self.assertRedirects(response,reverse('supervisor_dashboard'))
        response=self.client.get(reverse('backoffice_visits'),{'q':self.account.reference})
        self.assertContains(response,'&lt;script&gt;unsafe()&lt;/script&gt;');self.assertNotContains(response,'<script>unsafe()</script>')
        response=self.client.post(reverse('review_visit',args=[self.visit_id]),urlencode({'csrfmiddlewaretoken':'test-token','decision':'reject','reason':''}),content_type='application/x-www-form-urlencoded')
        self.assertEqual(response.status_code,400)
        response=self.client.post(reverse('review_visit',args=[self.visit_id]),urlencode({'csrfmiddlewaretoken':'test-token','decision':'approve','reason':'Reviewed'}),content_type='application/x-www-form-urlencoded')
        self.assertRedirects(response,reverse('backoffice_visits'))

    def test_failed_audit_rolls_back_review_and_released_history_is_reviewable(self):
        with patch('apps.activities.services.append_audit_event',side_effect=RuntimeError('test')),self.assertRaises(RuntimeError):
            review_visit(self.reviewer.pk,self.visit_id,'approve','')
        self.assertEqual(FieldActivity.objects.get(pk=self.visit_id).review_status,'pending')
        AccountAssignment.objects.filter(pk=self.assignment.pk).update(status='released',ended_at=timezone.now())
        review_visit(self.reviewer.pk,self.visit_id,'approve','Historical report checked')
        self.assertEqual(FieldActivity.objects.get(pk=self.visit_id).review_status,'approved')


class RevisitWorkflowTests(TestCase):
    setUp=VisitPersistenceTests.setUp
    begin=VisitPersistenceTests.begin
    def authorize(self, task, due):
        reviewer=get_user_model().objects.create_user(username='schedule-admin')
        reviewer.user_permissions.add(Permission.objects.get(codename='change_followuptask',content_type__app_label='activities'))
        ClientAccess.objects.create(user=reviewer,client=self.creditor)
        decide_followup(reviewer.pk,self.assignment.pk,'authorize',due.isoformat(),(due+timedelta(hours=1)).isoformat(),task_id=task['id'])

    def submitted(self):
        visit_id=self.begin()
        submit_visit(self.user.pk,self.assignment.pk,visit_id,completion())
        return visit_id

    def test_submission_blocks_immediate_repeat_and_call_does_not_unlock_visit(self):
        visit_id=self.submitted()
        with self.assertRaises(OfferWorkflowError) as caught:self.begin()
        self.assertEqual(caught.exception.code,'revisit_required')
        FollowUpTask.objects.create(account=self.account,assignment=self.assignment,source_activity_id=visit_id,assignee=self.user,kind='call',due_at=timezone.now()-timedelta(seconds=1))
        with self.assertRaises(OfferWorkflowError):self.begin()

    def test_schedule_retry_future_wait_and_completion_consumes_task(self):
        visit_id=self.submitted();due=timezone.now()+timedelta(hours=1)
        payload={'due_at':due.isoformat()}
        first=schedule_revisit(self.user.pk,self.assignment.pk,visit_id,payload)
        self.assertEqual(first['task'],schedule_revisit(self.user.pk,self.assignment.pk,visit_id,payload)['task'])
        self.assertEqual(visit_gate(self.assignment,self.user.pk,timezone.now())['state'],'requested')
        self.authorize(first['task'],due)
        with self.assertRaises(OfferWorkflowError) as caught:self.begin()
        self.assertEqual(caught.exception.code,'revisit_not_due')
        with patch('apps.activities.services.timezone.now',return_value=due):
            next_id=self.begin()
            self.assertEqual(FieldActivity.objects.get(pk=next_id).scheduled_followup_id,first['task']['id'])
            submit_visit(self.user.pk,self.assignment.pk,next_id,completion())
        task=FollowUpTask.objects.get(pk=first['task']['id'])
        self.assertEqual(task.status,'completed');self.assertEqual(task.completed_at,due)
        with self.assertRaises(IntegrityError),transaction.atomic():
            FieldActivity.objects.create(account=self.account,assignment=self.assignment,recorded_by=self.user,scheduled_followup=task,status='submitted',outcome='contacted',occurred_at=due,ended_at=due,location_issue_reason='No GPS',submission_fingerprint='b'*64)
        with self.assertRaises(OfferWorkflowError):self.begin()
        self.assignment.refresh_from_db();self.assertIsNone(self.assignment.next_action_due_at)

    def test_cancelled_revisit_can_retry_without_consuming_task(self):
        visit_id=self.submitted();due=timezone.now()+timedelta(hours=1)
        scheduled=schedule_revisit(self.user.pk,self.assignment.pk,visit_id,{'due_at':due.isoformat()})
        self.authorize(scheduled['task'],due)
        with patch('apps.activities.services.timezone.now',return_value=due):
            next_id=self.begin();cancel_visit(self.user.pk,self.assignment.pk,next_id,{'reason':'Phone battery failed'})
            retry_id=self.begin()
        self.assertNotEqual(next_id,retry_id)
        self.assertEqual(FollowUpTask.objects.get(pk=scheduled['task']['id']).status,'pending')

    def test_past_schedule_and_schedule_changes_are_rejected(self):
        visit_id=self.submitted()
        with self.assertRaises(OfferWorkflowError):schedule_revisit(self.user.pk,self.assignment.pk,visit_id,{'due_at':timezone.now().isoformat()})
        schedule_revisit(self.user.pk,self.assignment.pk,visit_id,{'due_at':(timezone.now()+timedelta(hours=1)).isoformat()})
        with self.assertRaises(OfferWorkflowError):schedule_revisit(self.user.pk,self.assignment.pk,visit_id,{'due_at':(timezone.now()+timedelta(hours=2)).isoformat()})


class FollowupPageTests(TestCase):
    def setUp(self):
        self.user,self.profile,self.creditor,self.account=offer_fixture()
        offer_id=refresh_offers(self.user.pk)['offers'][0]['id'];respond_to_offer(self.user.pk,offer_id,'accept')
        self.assignment=AccountAssignment.objects.get(account=self.account,status='active')
        self.now=datetime(2026,10,13,2,tzinfo=utc_timezone.utc)
        self.client.force_login(self.user)

    def task(self,due,status='pending',**values):
        return FollowUpTask.objects.create(account=self.account,assignment=self.assignment,assignee=self.user,kind='revisit',due_at=due,status=status,**values)

    def get_page(self,**params):
        with patch('apps.activities.views.timezone.now',return_value=self.now):
            return self.client.get(reverse('activities:followups'),params)

    def test_filters_search_and_summary_have_correct_boundaries(self):
        self.task(self.now-timedelta(seconds=1));self.task(self.now+timedelta(hours=1));self.task(followup_day_end(self.now))
        self.task(self.now,status='cancelled')
        response=self.get_page()
        self.assertEqual(response.context['counts'],{'total':3,'overdue':1,'today':1,'upcoming':1})
        for period in ['overdue','today','upcoming']:
            self.assertEqual(self.get_page(period=period).context['page_obj'].paginator.count,1)
        response=self.get_page(q='nothing matches')
        self.assertEqual(response.context['page_obj'].paginator.count,0)
        self.assertContains(response,'No matching follow-ups')
        self.assertEqual(self.get_page(q=self.account.reference).context['page_obj'].paginator.count,3)
        self.assertEqual(self.get_page(period='invalid',page='invalid').context['period'],'all')

    def test_old_ownership_and_revoked_access_hide_tasks(self):
        self.task(self.now)
        ClientAccess.objects.filter(user=self.user).update(is_active=False)
        self.assertEqual(self.get_page().context['counts']['total'],0)
        ClientAccess.objects.filter(user=self.user).update(is_active=True)
        AccountAssignment.objects.filter(pk=self.assignment.pk).update(status='released',ended_at=timezone.now())
        self.assertEqual(self.get_page().context['counts']['total'],0)

    def test_assignee_and_ownership_must_both_match(self):
        self.task(self.now)
        other=get_user_model().objects.create_user(username='followup-other')
        CollectorProfile.objects.create(user=other,approval_status='approved',approved_by=self.user,approved_at=timezone.now())
        ClientAccess.objects.create(user=other,client=self.creditor)
        FollowUpTask.objects.filter(assignment=self.assignment).update(assignee=other)
        self.assertEqual(self.get_page().context['counts']['total'],0)
        self.client.force_login(other)
        self.assertEqual(self.get_page().context['counts']['total'],0)

    def test_pagination_preserves_search_and_dashboard_uses_total(self):
        for index in range(13):self.task(self.now+timedelta(minutes=index))
        response=self.get_page(q=self.account.reference,page=2)
        self.assertEqual(len(response.context['tasks']),1)
        self.assertContains(response,'Page 2 of 2')
        self.assertContains(response,'q='+self.account.reference)
        with patch('apps.user.views.timezone.now',return_value=self.now):
            response=self.client.get(reverse('collector_dashboard'))
        self.assertTrue(response.context['follow_ups_connected'])
        self.assertEqual(response.context['follow_ups_total'],13)
        self.assertEqual(len(response.context['follow_ups']),5)
        self.assertContains(response,'View all follow-ups')


class FollowupAuthorizationValidationTests(SimpleTestCase):
    def test_permission_requires_active_authorized_user(self):
        self.assertFalse(can_schedule_followups(AnonymousUser()))
        for active,allowed,expected in [(True,False,False),(False,True,False),(True,True,True)]:
            user=SimpleNamespace(is_authenticated=True,is_active=active,is_staff=True,has_perm=lambda name:allowed)
            self.assertEqual(can_schedule_followups(user),expected)

    def test_window_rejects_past_equal_reversed_and_naive_dates(self):
        now=timezone.now();later=now+timedelta(hours=1)
        self.assertEqual(validate_schedule_window(now.isoformat(),later.isoformat(),now),(now,later))
        for start,end in [(now-timedelta(seconds=1),later),(later,later),(later,now)]:
            with self.assertRaises(OfferWorkflowError):validate_schedule_window(start.isoformat(),end.isoformat(),now)
        with self.assertRaises(OfferWorkflowError):validate_schedule_window('2026-10-13T12:00:00',later.isoformat(),now)

    def test_start_and_deadline_boundaries_and_legacy_behavior(self):
        now=timezone.now();task=SimpleNamespace(available_from=now,due_at=now+timedelta(hours=1))
        self.assertEqual(task_window_state(task,now-timedelta(microseconds=1)),'waiting')
        self.assertEqual(task_window_state(task,now),'ready')
        self.assertEqual(task_window_state(task,task.due_at),'ready')
        self.assertEqual(task_window_state(task,task.due_at+timedelta(microseconds=1)),'overdue')
        task.available_from=None
        self.assertEqual(task_window_state(task,task.due_at+timedelta(days=1)),'ready')

    def test_new_reports_only_request_revisits_and_legacy_retries_survive(self):
        now=timezone.now()
        for kind in ['call','promise_check']:
            payload=completion(outcome='promise_to_pay',promise={'amount':'10','due_date':(now+timedelta(days=1)).date().isoformat()},follow_up={'kind':kind,'due_at':(now+timedelta(days=1)).isoformat()})
            with self.assertRaises(OfferWorkflowError):validate_completion(payload,now)
            self.assertIsNotNone(validate_completion(payload,now,False)['follow_up'])

    def test_schedule_post_requires_login_csrf_and_permission(self):
        url=reverse('activities:authorize_followup',args=[1])
        self.assertEqual(self.client.post(url).status_code,302)
        self.assertEqual(Client(enforce_csrf_checks=True).post(url).status_code,403)
        request=RequestFactory().post(url);request.user=SimpleNamespace(is_authenticated=True,is_active=True,has_perm=lambda name:False)
        request._dont_enforce_csrf_checks=True
        from django.core.exceptions import PermissionDenied
        with self.assertRaises(PermissionDenied):views.schedule_followup_form(request,assignment_id=1)

    def test_native_form_translates_philippine_time_and_confirms_save(self):
        from django.contrib.messages.storage.fallback import FallbackStorage
        url=reverse('activities:authorize_followup',args=[1])
        data={'csrfmiddlewaretoken':'test','decision':'authorize','task_id':'2','available_from':'2026-10-14T09:00','due_at':'2026-10-14T10:00','note':'Checked'}
        request=RequestFactory().post(url,urlencode(data),content_type='application/x-www-form-urlencoded')
        request.user=SimpleNamespace(is_authenticated=True,pk=3);request._dont_enforce_csrf_checks=True
        request.session={};request._messages=FallbackStorage(request)
        with patch('apps.activities.views._scheduling_context',return_value={}),patch('apps.activities.views.decide_followup',return_value={'saved':True,'task':{'status':'pending'}}) as service:
            response=views.schedule_followup_form(request,assignment_id=1)
        self.assertEqual(response.status_code,302)
        service.assert_called_once_with(3,1,'authorize','2026-10-14T01:00:00+00:00','2026-10-14T02:00:00+00:00','Checked',2)


class FollowupAuthorizationTests(TestCase):
    setUpFixture=VisitPersistenceTests.setUp
    begin=VisitPersistenceTests.begin

    def setUp(self):
        self.setUpFixture()
        self.visit_id=self.begin();submit_visit(self.user.pk,self.assignment.pk,self.visit_id,completion())
        self.admin=get_user_model().objects.create_user(username='followup-scheduler')
        self.permission=Permission.objects.get(codename='change_followuptask',content_type__app_label='activities')
        self.admin.user_permissions.add(self.permission)
        ClientAccess.objects.create(user=self.admin,client=self.creditor)
        self.start=timezone.now()+timedelta(hours=1);self.deadline=self.start+timedelta(hours=2)

    def request_visit(self):
        return schedule_revisit(self.user.pk,self.assignment.pk,self.visit_id,{'due_at':self.start.isoformat()})['task']['id']

    def authorize(self,task_id=None):
        return decide_followup(self.admin.pk,self.assignment.pk,'authorize',self.start.isoformat(),self.deadline.isoformat(),'Scheduled',task_id)

    def test_request_never_unlocks_or_sets_effective_assignment_deadline(self):
        task_id=self.request_visit()
        self.assertEqual(self.request_visit(),task_id)
        self.assignment.refresh_from_db();self.assertIsNone(self.assignment.next_action_due_at)
        task=FollowUpTask.objects.get(pk=task_id)
        self.assertEqual(task.status,'requested');self.assertEqual(task.requested_by_id,self.user.pk)
        self.assertIsNone(task.authorized_by_id)
        with patch('apps.activities.services.timezone.now',return_value=self.deadline+timedelta(days=1)),self.assertRaises(OfferWorkflowError):self.begin()
        self.assertEqual(AuditEvent.objects.filter(action='followup_requested').count(),1)

    def test_authorization_is_independent_repeat_safe_and_uses_window(self):
        task_id=self.request_visit();self.authorize(task_id)
        self.assertFalse(self.authorize(task_id)['changed'])
        task=FollowUpTask.objects.get(pk=task_id)
        self.assertEqual(task.authorized_by_id,self.admin.pk)
        self.assertEqual(FieldActivity.objects.get(pk=self.visit_id).review_status,'pending')
        self.assertEqual(AuditEvent.objects.filter(action='followup_authorized').count(),1)
        self.assertFalse(visit_gate(self.assignment,self.user.pk,self.start-timedelta(seconds=1))['allowed'])
        self.assertTrue(visit_gate(self.assignment,self.user.pk,self.start)['allowed'])
        with patch('apps.activities.services.timezone.now',return_value=self.deadline+timedelta(seconds=1)),self.assertRaises(OfferWorkflowError):self.begin()
        with patch('apps.activities.services.timezone.now',return_value=self.start):next_id=self.begin()
        with patch('apps.activities.services.timezone.now',return_value=self.deadline+timedelta(minutes=1)):
            submit_visit(self.user.pk,self.assignment.pk,next_id,completion())
        task.refresh_from_db();self.assertEqual(task.status,'completed');self.assertGreater(task.completed_at,task.due_at)
        self.account.refresh_from_db();self.assertEqual(self.account.balance_snapshot,Decimal('1000'))

    def test_admin_can_schedule_without_request_and_cannot_replace_existing(self):
        first=self.authorize();self.assertEqual(first['task']['status'],'pending')
        self.assertFalse(self.authorize()['changed'])
        with self.assertRaises(OfferWorkflowError):
            decide_followup(self.admin.pk,self.assignment.pk,'authorize',(self.start+timedelta(minutes=1)).isoformat(),self.deadline.isoformat())
        self.assertEqual(FollowUpTask.objects.count(),1)

    def test_rejection_note_visible_and_new_request_is_allowed(self):
        task_id=self.request_visit()
        with self.assertRaises(OfferWorkflowError):decide_followup(self.admin.pk,self.assignment.pk,'reject',note='',task_id=task_id)
        decide_followup(self.admin.pk,self.assignment.pk,'reject',note='Please propose another day',task_id=task_id)
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse('assignments:account_detail',args=[self.assignment.pk])),'Please propose another day')
        self.assertNotEqual(self.request_visit(),task_id)

    def test_permission_scope_ownership_self_authorization_and_audit_rollback(self):
        task_id=self.request_visit()
        self.user.user_permissions.add(self.permission)
        with self.assertRaises(OfferWorkflowError) as caught:
            decide_followup(self.user.pk,self.assignment.pk,'authorize',self.start.isoformat(),self.deadline.isoformat(),task_id=task_id)
        self.assertEqual(caught.exception.code,'self_schedule')
        ClientAccess.objects.filter(user=self.admin).update(is_active=False)
        with self.assertRaises(OfferWorkflowError):self.authorize(task_id)
        ClientAccess.objects.filter(user=self.admin).update(is_active=True)
        with patch('apps.activities.services.append_audit_event',side_effect=RuntimeError('test failure')),self.assertRaises(RuntimeError):self.authorize(task_id)
        self.assertEqual(FollowUpTask.objects.get(pk=task_id).status,'requested')
        self.admin.user_permissions.clear()
        with self.assertRaises(OfferWorkflowError):self.authorize(task_id)
        self.admin.user_permissions.add(self.permission)
        AccountAssignment.objects.filter(pk=self.assignment.pk).update(status='released',ended_at=timezone.now())
        with self.assertRaises(OfferWorkflowError):self.authorize(task_id)

    def test_scheduler_queue_and_login_do_not_require_visit_review_permission(self):
        self.request_visit();self.client.force_login(self.admin)
        response=self.client.get(reverse('supervisor_dashboard'))
        self.assertEqual(response.context['followup_request_count'],1)
        response=self.client.get(reverse('backoffice_followups'))
        self.assertContains(response,'Collector requests');self.assertEqual(len(response.context['schedule_requests']),1)
        self.assertEqual(self.client.get(reverse('backoffice_visits')).status_code,403)
        self.assertRedirects(self.client.get(reverse('login_success')),reverse('supervisor_dashboard'))

    def test_legacy_pending_schedule_keeps_no_invented_authorization(self):
        task=FollowUpTask.objects.create(account=self.account,assignment=self.assignment,source_activity_id=self.visit_id,assignee=self.user,kind='revisit',status='pending',due_at=self.start)
        self.assertIsNone(task.authorized_by_id);self.assertIsNone(task.available_from)
        self.assertTrue(visit_gate(self.assignment,self.user.pk,self.deadline+timedelta(days=1))['allowed'])
        with self.assertRaises(OfferWorkflowError):self.authorize()


class FollowupAuthorizationConcurrencyTests(TransactionTestCase):
    def test_competing_schedulers_cannot_overwrite_window(self):
        user,profile,creditor,account=offer_fixture()
        offer_id=refresh_offers(user.pk)['offers'][0]['id'];respond_to_offer(user.pk,offer_id,'accept')
        assignment=AccountAssignment.objects.get(account=account,status='active')
        activity=start_visit(user.pk,assignment.pk,start_payload())['visit']['id']
        submit_visit(user.pk,assignment.pk,activity,completion())
        start=timezone.now()+timedelta(hours=1);deadline=start+timedelta(hours=1)
        task_id=schedule_revisit(user.pk,assignment.pk,activity,{'due_at':start.isoformat()})['task']['id']
        permission=Permission.objects.get(codename='change_followuptask',content_type__app_label='activities')
        admins=[]
        for name in ['schedule-first','schedule-second']:
            admin=get_user_model().objects.create_user(username=name);admin.user_permissions.add(permission)
            ClientAccess.objects.create(user=admin,client=creditor);admins.append(admin.pk)
        barrier=Barrier(2,timeout=10)
        def authorize(admin_id):
            close_old_connections()
            try:
                barrier.wait()
                return decide_followup(admin_id,assignment.pk,'authorize',start.isoformat(),deadline.isoformat(),task_id=task_id)
            except OfferWorkflowError as exc:return {'error':exc.code}
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(authorize,admin_id) for admin_id in admins]
            results=[future.result(timeout=20) for future in futures]
        self.assertEqual(sum(r.get('saved') is True for r in results),1)
        self.assertEqual(AuditEvent.objects.filter(action='followup_authorized').count(),1)
        self.assertEqual(FollowUpTask.objects.get(pk=task_id).status,'pending')


class BackOfficePortalTests(SimpleTestCase):
    def user(self, review=False, schedule=False):
        allowed=set()
        if review:allowed.add('activities.change_fieldactivity')
        if schedule:allowed.add('activities.change_followuptask')
        return SimpleNamespace(is_authenticated=True,is_active=True,pk=1,username='reviewer',first_name='',get_full_name=lambda:'Reviewer',has_perm=lambda name:name in allowed)

    def request(self,url,user):
        request=RequestFactory().get(url);request.user=user
        return request

    def test_pages_require_login_and_only_get(self):
        for name,view in [('supervisor_dashboard',views.review_dashboard),('backoffice_visits',views.backoffice_visits),('backoffice_followups',views.backoffice_followups)]:
            url=reverse(name)
            self.assertEqual(self.client.get(url).status_code,302)
            request=RequestFactory().post(url);request.user=self.user(True,True)
            self.assertEqual(view(request).status_code,405)

    def test_permissions_protect_direct_page_and_post_access(self):
        from django.core.exceptions import PermissionDenied
        for view,user in [(views.backoffice_visits,self.user(schedule=True)),(views.backoffice_followups,self.user(review=True)),(views.review_dashboard,self.user())]:
            with self.assertRaises(PermissionDenied):view(self.request('/back-office/',user))
        request=RequestFactory().post('/review/');request.user=self.user(schedule=True);request._dont_enforce_csrf_checks=True
        with self.assertRaises(PermissionDenied):views.review_visit_form(request,activity_id=1)
        request=RequestFactory().post('/schedule/');request.user=self.user(review=True);request._dont_enforce_csrf_checks=True
        with self.assertRaises(PermissionDenied):views.schedule_followup_form(request,assignment_id=1)

    def test_dashboard_queries_only_permitted_client_scoped_queues(self):
        request=self.request('/back-office/',self.user(review=True))
        with patch('apps.activities.views.reviewable_visits') as reports,patch('apps.activities.views._schedule_queries') as scheduling:
            reports.return_value.filter.return_value.count.return_value=4
            response=views.review_dashboard(request)
            reports.assert_called_once_with(request.user);reports.return_value.filter.assert_called_once_with(review_status='pending')
            scheduling.assert_not_called()
        html=response.content.decode()
        self.assertIn('Reports awaiting review',html);self.assertIn('<strong>4</strong>',html)
        self.assertNotIn('Set visit schedules',html);self.assertNotIn('Approve report',html)
        self.assertNotIn('Follow-up Scheduling</a>',html)
        self.assertEqual(response.headers['Cache-Control'],'max-age=0, no-cache, no-store, must-revalidate, private')

    def test_scheduler_dashboard_is_summary_and_navigation_is_scoped(self):
        request=self.request('/back-office/',self.user(schedule=True))
        queues=[SimpleNamespace(),SimpleNamespace(count=lambda:3),SimpleNamespace(count=lambda:2)]
        with patch('apps.activities.views._schedule_queries',return_value=queues) as scheduling,patch('apps.activities.views.reviewable_visits') as reviews:
            response=views.review_dashboard(request)
            scheduling.assert_called_once_with(request.user);reviews.assert_not_called()
        html=response.content.decode()
        self.assertIn('<strong>3</strong>',html);self.assertIn('<strong>2</strong>',html)
        self.assertNotIn('Visit Reviews</a>',html)
        self.assertNotIn('Authorize schedule</button>',html)
        self.assertIn('aria-current="page"',html)

    def test_template_page_separation_escaped_evidence_and_asset_loading(self):
        from django.template.loader import render_to_string
        page=Paginator([],10).get_page(1)
        user=self.user(True,True);request=self.request('/back-office/visits/',user)
        context={'can_review':True,'can_schedule':True,'page_title':'Visit Reviews','backoffice_section':'visits','reports':page,'page_obj':page,'review_counts':{'pending':0,'approved':0,'rejected':0,'legacy':0},'review_state':'pending','search_query':'<script>bad()</script>'}
        html=render_to_string('backoffice/visits.html',context,request=request)
        self.assertIn('&lt;script&gt;bad()&lt;/script&gt;',html)
        self.assertNotIn('Collector requests',html)
        self.assertIn('css/backoffice/portal.css',html);self.assertIn('js/backoffice/navigation.js',html)
        self.assertNotIn('js/components/sidebar.js',html)
        context.update(page_title='Follow-up Scheduling',backoffice_section='followups',schedule_requests=page,schedule_assignments=page,overdue_schedules=page)
        html=render_to_string('backoffice/followups.html',context,request=request)
        self.assertIn('Collector requests',html);self.assertNotIn('Find a report',html)
        self.assertIn('name="csrfmiddlewaretoken"',html)


class BackOfficePortalScopeTests(TestCase):
    setUp=BackOfficeReviewTests.setUp

    def test_dashboard_and_queues_match_and_revoked_scope_hides_records(self):
        self.reviewer.user_permissions.add(Permission.objects.get(codename='change_followuptask',content_type__app_label='activities'))
        due=timezone.now()+timedelta(hours=1)
        schedule_revisit(self.user.pk,self.assignment.pk,self.visit_id,{'due_at':due.isoformat()})
        self.client.force_login(self.reviewer)
        response=self.client.get(reverse('supervisor_dashboard'))
        self.assertEqual(response.context['pending_reports'],1);self.assertEqual(response.context['followup_request_count'],1)
        self.assertEqual(self.client.get(reverse('backoffice_visits')).context['page_obj'].paginator.count,1)
        self.assertEqual(self.client.get(reverse('backoffice_followups')).context['schedule_requests'].paginator.count,1)
        ClientAccess.objects.filter(user=self.reviewer).update(is_active=False)
        response=self.client.get(reverse('supervisor_dashboard'))
        self.assertEqual(response.context['pending_reports'],0);self.assertEqual(response.context['followup_request_count'],0)
        self.assertEqual(self.client.get(reverse('backoffice_visits')).context['page_obj'].paginator.count,0)
        self.assertEqual(self.client.get(reverse('backoffice_followups')).context['schedule_requests'].paginator.count,0)

    def test_successful_posts_return_to_their_work_queue(self):
        self.reviewer.user_permissions.add(Permission.objects.get(codename='change_followuptask',content_type__app_label='activities'))
        self.client.force_login(self.reviewer)
        response=self.client.post(reverse('review_visit',args=[self.visit_id]),urlencode({'csrfmiddlewaretoken':'test','decision':'approve','reason':'Checked'}),content_type='application/x-www-form-urlencoded')
        self.assertRedirects(response,reverse('backoffice_visits'))
        start=(timezone.now()+timedelta(hours=1)).astimezone(ZoneInfo('Asia/Manila'))
        data={'csrfmiddlewaretoken':'test','decision':'authorize','task_id':'','available_from':start.strftime('%Y-%m-%dT%H:%M'),'due_at':(start+timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M'),'note':''}
        response=self.client.post(reverse('activities:authorize_followup',args=[self.assignment.pk]),urlencode(data),content_type='application/x-www-form-urlencoded')
        self.assertRedirects(response,reverse('backoffice_followups'))


class CollectorVisitGateTests(SimpleTestCase):
    def test_ongoing_visit_elsewhere_blocks_without_reading_followup_tasks(self):
        assignment=SimpleNamespace(pk=1,account_id=1)
        with patch('apps.activities.services.FieldActivity.objects') as records,patch('apps.activities.services.FollowUpTask.objects') as tasks:
            records.filter.return_value.order_by.return_value.first.return_value=None
            records.filter.return_value.exclude.return_value.exists.return_value=True
            gate=visit_gate(assignment,7,timezone.now())
            self.assertFalse(gate['allowed']);self.assertEqual(gate['state'],'another_visit')
            records.filter.assert_any_call(recorded_by_id=7,kind='visit',status='in_progress')
            records.filter.return_value.exclude.assert_called_once_with(assignment=assignment)
            tasks.filter.assert_not_called()


def another_assignment(assignment):
    from apps.accounts.models import Account
    source=assignment.account
    account=Account.objects.create(client=source.client,customer=source.customer,source_system=source.source_system,
        external_id='second-'+str(uuid4()),reference='SECOND-'+str(uuid4()),balance_snapshot=source.balance_snapshot,
        balance_as_of=source.balance_as_of,collection_address=source.collection_address,commission_rule=source.commission_rule)
    return AccountAssignment.objects.create(account=account,collector=assignment.collector,target_address=assignment.target_address,
        commission_rule=assignment.commission_rule,target_position=assignment.target_position,policy_snapshot=assignment.policy_snapshot,
        first_action_due_at=timezone.now()+timedelta(hours=24))


class SingleCollectorVisitTests(TestCase):
    setUp=VisitPersistenceTests.setUp
    begin=VisitPersistenceTests.begin

    def test_second_account_blocked_until_submission_preserves_both_assignments(self):
        other=another_assignment(self.assignment)
        current=self.begin()
        with self.assertRaises(OfferWorkflowError) as caught:start_visit(self.user.pk,other.pk,start_payload())
        self.assertEqual(caught.exception.code,'collector_visit_in_progress')
        self.client.force_login(self.user)
        response=self.client.get(reverse('assignments:account_detail',args=[other.pk]))
        self.assertContains(response,'Finish or cancel your current visit')
        self.assertFalse(response.context['visit_gate']['allowed'])
        submit_visit(self.user.pk,self.assignment.pk,current,completion())
        next_visit=start_visit(self.user.pk,other.pk,start_payload())
        self.assertEqual(next_visit['visit']['status'],'in_progress')
        self.assertEqual(AccountAssignment.objects.filter(collector=self.profile,status='active').count(),2)

    def test_cancellation_unlocks_other_account_and_same_account_retries_resume(self):
        other=another_assignment(self.assignment);payload=start_payload()
        first=start_visit(self.user.pk,self.assignment.pk,payload)
        self.assertEqual(start_visit(self.user.pk,self.assignment.pk,payload)['visit'],first['visit'])
        self.assertEqual(self.begin(),first['visit']['id'])
        cancel_visit(self.user.pk,self.assignment.pk,first['visit']['id'],{'reason':'Customer requested rescheduling'})
        self.assertEqual(start_visit(self.user.pk,other.pk,start_payload())['visit']['status'],'in_progress')

    def test_database_constraint_blocks_direct_second_insert(self):
        other=another_assignment(self.assignment);self.begin()
        with self.assertRaises(IntegrityError),transaction.atomic():
            FieldActivity.objects.create(account=other.account,assignment=other,recorded_by=self.user,occurred_at=timezone.now(),location_issue_reason='No location signal')
        self.assertEqual(FieldActivity.objects.filter(recorded_by=self.user,status='in_progress',kind='visit').count(),1)


class SingleCollectorVisitConcurrencyTests(TransactionTestCase):
    setUp=VisitPersistenceTests.setUp

    def test_simultaneous_starts_on_different_accounts_allow_only_one(self):
        other=another_assignment(self.assignment);barrier=Barrier(2,timeout=10)
        def begin(assignment_id):
            close_old_connections()
            try:
                barrier.wait()
                return start_visit(self.user.pk,assignment_id,start_payload())
            except OfferWorkflowError as exc:return {'error':exc.code}
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(begin,assignment_id) for assignment_id in [self.assignment.pk,other.pk]]
            results=[future.result(timeout=20) for future in futures]
        self.assertEqual(sum(r.get('saved') is True for r in results),1)
        self.assertEqual(sum(r.get('error')=='collector_visit_in_progress' for r in results),1)
        self.assertEqual(FieldActivity.objects.filter(recorded_by=self.user,status='in_progress',kind='visit').count(),1)


class ContactAttemptTests(TestCase):
    def setUp(self):
        self.user, self.profile, self.creditor, self.account = offer_fixture()
        offer_id = refresh_offers(self.user.pk)["offers"][0]["id"]
        respond_to_offer(self.user.pk, offer_id, "accept")
        self.assignment = AccountAssignment.objects.get(account=self.account, status="active")
        self.endpoint = reverse("activities:record_contact_attempt", args=[self.assignment.pk])

    def payload(self, **overrides):
        data = {
            "event_id": str(uuid4()),
            "kind": "phone",
            "outcome": "contacted",
            "notes": "Called customer. Spoke briefly regarding balance.",
        }
        data.update(overrides)
        return data

    def test_sms_kind_accepted_in_model_state(self):
        now = timezone.now()
        act = FieldActivity(
            account=self.account,
            assignment=self.assignment,
            recorded_by=self.user,
            event_id=uuid4(),
            kind="sms",
            status=FieldActivity.Status.SUBMITTED,
            review_status="not_required",
            outcome="contacted",
            notes="Sent SMS payment reminder.",
            occurred_at=now,
            ended_at=now,
            submission_fingerprint="dummy_fp",
        )
        act.full_clean()
        act.save()
        self.assertEqual(FieldActivity.objects.filter(kind="sms").count(), 1)
        saved = FieldActivity.objects.get(kind="sms")
        self.assertEqual(saved.get_kind_display(), "SMS")

    def test_valid_call_sms_and_email_attempts_save_as_submitted_review_not_required(self):
        for kind in ["phone", "sms", "email"]:
            with self.subTest(kind=kind):
                payload = self.payload(kind=kind, notes=f"Attempt via {kind}")
                res = record_contact_attempt(self.user.pk, self.assignment.pk, payload)
                self.assertTrue(res["saved"])
                self.assertTrue(res["created"])
                attempt_id = res["attempt"]["id"]

                activity = FieldActivity.objects.get(pk=attempt_id)
                self.assertEqual(activity.kind, kind)
                self.assertEqual(activity.status, FieldActivity.Status.SUBMITTED)
                self.assertEqual(activity.review_status, "not_required")
                self.assertEqual(activity.outcome, "contacted")
                self.assertEqual(activity.notes, f"Attempt via {kind}")
                self.assertIsNone(activity.position)
                self.assertIsNone(activity.accuracy_m)
                self.assertIsNone(activity.distance_m)
                self.assertIsNone(activity.location_captured_at)
                self.assertEqual(activity.location_issue_reason, "")
                self.assertIsNotNone(activity.occurred_at)
                self.assertIsNotNone(activity.ended_at)
                self.assertEqual(activity.occurred_at, activity.ended_at)
                self.assertEqual(PromiseToPay.objects.count(), 0)
                self.assertEqual(FollowUpTask.objects.count(), 0)

    def test_invalid_payloads_and_unsupported_outcomes_rejected(self):
        # Extra keys
        with self.assertRaises(OfferWorkflowError) as caught:
            record_contact_attempt(self.user.pk, self.assignment.pk, self.payload(extra="forbidden"))
        self.assertEqual(caught.exception.status, 400)

        # Missing keys
        incomplete = {"event_id": str(uuid4()), "kind": "phone", "outcome": "contacted"}
        with self.assertRaises(OfferWorkflowError) as caught:
            record_contact_attempt(self.user.pk, self.assignment.pk, incomplete)
        self.assertEqual(caught.exception.status, 400)

        # Invalid UUID
        with self.assertRaises(OfferWorkflowError) as caught:
            record_contact_attempt(self.user.pk, self.assignment.pk, self.payload(event_id="bad-uuid"))
        self.assertEqual(caught.exception.status, 400)

        # Invalid types
        for bad_id in [123, None, True, ["uuid"]]:
            with self.assertRaises(OfferWorkflowError) as caught:
                record_contact_attempt(self.user.pk, self.assignment.pk, self.payload(event_id=bad_id))
            self.assertEqual(caught.exception.status, 400)

        # Blank or whitespace notes
        for bad_note in ["", "   ", " \n \t "]:
            with self.assertRaises(OfferWorkflowError) as caught:
                record_contact_attempt(self.user.pk, self.assignment.pk, self.payload(notes=bad_note))
            self.assertEqual(caught.exception.status, 400)

        # Notes exceeding 5,000 chars
        with self.assertRaises(OfferWorkflowError) as caught:
            record_contact_attempt(self.user.pk, self.assignment.pk, self.payload(notes="x" * 5001))
        self.assertEqual(caught.exception.status, 400)

        # Unsupported method
        for bad_kind in ["visit", "skip_trace", "carrier_pigeon", "Call", "SMS"]:
            with self.assertRaises(OfferWorkflowError) as caught:
                record_contact_attempt(self.user.pk, self.assignment.pk, self.payload(kind=bad_kind))
            self.assertEqual(caught.exception.status, 400)

        # Unsupported outcome
        for bad_outcome in ["promise_to_pay", "wrong_address", "invalid", "Contacted"]:
            with self.assertRaises(OfferWorkflowError) as caught:
                record_contact_attempt(self.user.pk, self.assignment.pk, self.payload(outcome=bad_outcome))
            self.assertEqual(caught.exception.status, 400)

    def test_authorization_checks(self):
        payload = self.payload()

        # 1. Unauthenticated -> 401
        res = self.client.post(self.endpoint, data=json.dumps(payload), content_type="application/json")
        self.assertEqual(res.status_code, 401)

        # 2. Back Office user -> 403
        from django.contrib.auth import get_user_model
        bo_user = get_user_model().objects.create_user(username="bo_user", password="password", is_staff=True)
        self.client.force_login(bo_user)
        res = self.client.post(self.endpoint, data=json.dumps(payload), content_type="application/json")
        self.assertEqual(res.status_code, 403)

        # 3. Another collector -> 404
        other_user, _, _, _ = offer_fixture(username="other_col", email="other@example.com")
        self.client.force_login(other_user)
        res = self.client.post(self.endpoint, data=json.dumps(payload), content_type="application/json")
        self.assertEqual(res.status_code, 404)

        # 4. Inactive assignment -> 404
        self.client.force_login(self.user)
        self.assignment.status = "released"
        self.assignment.save(update_fields=["status"])
        res = self.client.post(self.endpoint, data=json.dumps(payload), content_type="application/json")
        self.assertEqual(res.status_code, 404)
        self.assignment.status = "active"
        self.assignment.save(update_fields=["status"])

        # 5. Revoked client access -> 404
        ClientAccess.objects.filter(user=self.user, client=self.account.client).update(is_active=False)
        res = self.client.post(self.endpoint, data=json.dumps(payload), content_type="application/json")
        self.assertEqual(res.status_code, 404)
        ClientAccess.objects.filter(user=self.user, client=self.account.client).update(is_active=True)

    def test_exact_retry_and_changed_retry_conflict(self):
        payload = self.payload()
        first = record_contact_attempt(self.user.pk, self.assignment.pk, payload)
        self.assertTrue(first["saved"])
        self.assertTrue(first["created"])

        # Exact retry returns existing without creating duplicate
        retry = record_contact_attempt(self.user.pk, self.assignment.pk, payload)
        self.assertTrue(retry["saved"])
        self.assertFalse(retry["created"])
        self.assertEqual(first["attempt"]["id"], retry["attempt"]["id"])
        self.assertEqual(FieldActivity.objects.filter(kind="phone").count(), 1)

        # Changed notes with same event_id -> 409
        with self.assertRaises(OfferWorkflowError) as caught:
            record_contact_attempt(self.user.pk, self.assignment.pk, {**payload, "notes": "Different note"})
        self.assertEqual(caught.exception.status, 409)

        # Changed outcome with same event_id -> 409
        with self.assertRaises(OfferWorkflowError) as caught:
            record_contact_attempt(self.user.pk, self.assignment.pk, {**payload, "outcome": "unavailable"})
        self.assertEqual(caught.exception.status, 409)

        # Changed kind with same event_id -> 409
        with self.assertRaises(OfferWorkflowError) as caught:
            record_contact_attempt(self.user.pk, self.assignment.pk, {**payload, "kind": "sms"})
        self.assertEqual(caught.exception.status, 409)

    def test_audit_event_creation(self):
        payload = self.payload(kind="email", outcome="disputed", notes="Customer disputed via email.")
        res = record_contact_attempt(self.user.pk, self.assignment.pk, payload)
        activity_id = res["attempt"]["id"]

        audit = AuditEvent.objects.filter(action="contact_attempt_recorded", entity_id=str(activity_id)).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.actor, self.user)
        self.assertEqual(audit.entity_type, "FieldActivity")
        self.assertEqual(audit.account, self.account)
        self.assertEqual(audit.changes.get("kind"), "email")
        self.assertEqual(audit.changes.get("outcome"), "disputed")
        self.assertEqual(audit.changes.get("status"), "submitted")

    def test_post_only_endpoint(self):
        self.client.force_login(self.user)
        get_res = self.client.get(self.endpoint)
        self.assertEqual(get_res.status_code, 405)
        self.assertEqual(get_res.headers.get("Allow"), "POST")

        put_res = self.client.put(self.endpoint, data="{}", content_type="application/json")
        self.assertEqual(put_res.status_code, 405)

        delete_res = self.client.delete(self.endpoint)
        self.assertEqual(delete_res.status_code, 405)

        valid_res = self.client.post(self.endpoint, data=json.dumps(self.payload()), content_type="application/json")
        self.assertEqual(valid_res.status_code, 200)
        data = valid_res.json()
        self.assertTrue(data.get("saved"))
        self.assertTrue(data.get("created"))

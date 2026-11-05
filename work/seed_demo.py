"""Seed fictional local workflow data; reruns preserve existing demo work."""
import struct
import zlib
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from django.contrib.gis.geos import Point
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.utils import timezone
from apps.accounts.models import Client, Customer, CustomerAddress, Account
from apps.user.models import User, CollectorProfile, ClientAccess
from apps.collection.models import CommissionRule, Payment
from apps.collection.services import report_payment
from apps.assignments.models import AccountAssignment, AssignmentReleaseRequest, AccountOffer
from apps.assignments.services import (
    assign_or_reassign_account, compute_ownership_token, refresh_offers,
    request_assignment_release, submit_collector_location, change_availability,
)
from apps.activities.models import FieldActivity, FollowUpTask
from apps.activities.services import start_visit, submit_visit


@transaction.atomic
def seed():
    now = timezone.now()
    collector = User.objects.get(username='collector', is_active=True)
    reviewer = User.objects.get(username='backoffice', is_active=True)
    profile = CollectorProfile.objects.get(user=collector, approval_status='approved')
    client, _ = Client.objects.get_or_create(code='DEMO-TEST', defaults={
        'name': 'Demo Collection Client (Fictional)', 'allow_collector_release': True,
        'matching_strategy': 'nearest',
    })
    for user in (collector, reviewer):
        ClientAccess.objects.update_or_create(user=user, client=client, defaults={'is_active': True})
    rule, _ = CommissionRule.objects.get_or_create(client=client, code='DEMO-10PCT', version=1, defaults={
        'name': 'Demo 10% verified-payment commission', 'kind': 'percentage',
        'percentage': Decimal('10'), 'earning_basis': 'verified_payment',
    })
    # Use an existing collector position when available, otherwise central Manila.
    existing_location = getattr(profile, 'location', None)
    origin = existing_location.position if existing_location else Point(120.9842, 14.5995, srid=4326)
    accounts = []
    for i in range(1, 9):
        customer, _ = Customer.objects.get_or_create(
            client=client, source_system='TEST', external_id=f'DEMO-CUST-{i:03}',
            defaults={'display_name': f'Demo Customer {i:02} (Fictional)'},
        )
        address, _ = CustomerAddress.objects.get_or_create(customer=customer, address_line=f'{i} Demo Street, Fictional Test Address', defaults={
            'kind': 'residential', 'position': Point(origin.x + i * 0.0003, origin.y + i * 0.0002, srid=4326),
            'verification_status': 'verified', 'verified_by': reviewer, 'verified_at': now,
        })
        account, _ = Account.objects.get_or_create(client=client, source_system='TEST', external_id=f'DEMO-ACC-{i:03}', defaults={
            'customer': customer, 'reference': f'DEMO-ACC-{i:03}',
            'balance_snapshot': Decimal(3000 + i * 1500), 'balance_as_of': now,
            'collection_address': address, 'commission_rule': rule, 'priority': (i % 5) + 1,
        })
        accounts.append(account)
    assignments = []
    for account in accounts[:4]:
        assignment = AccountAssignment.objects.filter(account=account, status='active').first()
        if assignment is None:
            assignment = assign_or_reassign_account(
                reviewer.pk, account.pk, profile.pk, 'Fictional demo setup',
                str(uuid4()), compute_ownership_token(account),
            )['assignment']
        assignments.append(assignment)
    for i, assignment in enumerate(assignments[:2]):
        if not FieldActivity.objects.filter(assignment=assignment).exists():
            visit = start_visit(collector.pk, assignment.pk, {
                'event_id': str(uuid4()), 'location': None,
                'location_issue_reason': 'Fictional demo visit; no real device GPS evidence.',
            })['visit']
            submit_visit(collector.pk, assignment.pk, visit['id'], {
                'outcome': 'promise_to_pay' if i == 0 else 'unavailable',
                'notes': 'Fictional test report for Back Office review.',
                'promise': {'amount': '1500.00', 'due_date': (timezone.localdate() + timedelta(days=3)).isoformat()} if i == 0 else None,
                'follow_up': {'kind': 'revisit', 'due_at': (now + timedelta(days=i + 1)).isoformat()},
            })
    # One scheduled task and one request awaiting a Back Office decision.
    task = FollowUpTask.objects.filter(assignment=assignments[0], status='requested').first()
    if task:
        task.status = 'pending'
        task.available_from = now
        task.due_at = now + timedelta(hours=6)
        task.authorized_by = reviewer
        task.authorized_at = now
        task.decision_note = 'Fictional demo schedule.'
        task.full_clean()
        task.save()
    assignment = assignments[0]
    if not Payment.objects.filter(account=assignment.account, reference='DEMO-PAY-001').exists():
        # Valid, explicitly fictional image receipt uploaded through the normal service.
        def chunk(kind, data):
            return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data) & 0xffffffff)
        png = (b'\x89PNG\r\n\x1a\n'
               + chunk(b'IHDR', struct.pack('!2I5B', 200, 100, 8, 2, 0, 0, 0))
               + chunk(b'tEXt', b'Description\x00FICTIONAL DEMO PLACEHOLDER - NOT A REAL RECEIPT')
               + chunk(b'IDAT', zlib.compress((b'\x00' + b'\xdd\xee\xff' * 200) * 100))
               + chunk(b'IEND', b''))
        report_payment(collector.pk, assignment.pk, {
            'event_id': str(uuid4()), 'amount': '1000.00', 'paid_at': now.isoformat(),
            'method': 'e_wallet', 'reference': 'DEMO-PAY-001',
            'notes': 'FICTIONAL demo payment. Image is a test placeholder, not a real receipt.',
        }, SimpleUploadedFile('fictional-demo-receipt.png', png, content_type='image/png'))
    if not AssignmentReleaseRequest.objects.filter(assignment=assignments[3]).exists():
        request_assignment_release(collector.pk, assignments[3].pk, {
            'event_id': str(uuid4()), 'reason': 'Fictional demo request: collector cannot cover this area.',
        })
    # Refresh the existing demo position without claiming real device evidence.
    submit_collector_location(collector.pk, {
        'longitude': origin.x, 'latitude': origin.y, 'accuracy_m': 20,
        'captured_at': timezone.now().isoformat(),
    })
    change_availability(collector.pk, 'online')
    refresh_offers(collector.pk)
    for account in accounts:
        account.full_clean()
    print({
        'client': client.code, 'customers': client.customers.count(), 'accounts': client.accounts.count(),
        'assignments': AccountAssignment.objects.filter(account__client=client, status='active').count(),
        'pending_offers': AccountOffer.objects.filter(account__client=client, status='pending').count(),
        'visit_reports': FieldActivity.objects.filter(account__client=client).count(),
        'followups': FollowUpTask.objects.filter(account__client=client).count(),
        'payments': Payment.objects.filter(account__client=client).count(),
        'release_requests': AssignmentReleaseRequest.objects.filter(assignment__account__client=client).count(),
    })


seed()

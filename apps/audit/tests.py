from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from .models import AuditEvent
from .services import append_audit_event, validate_audit_changes


class AuditPayloadTests(SimpleTestCase):
    def test_safe_before_after_evidence_is_accepted(self):
        changes = {"availability": {"before": "offline", "after": "online"}}
        self.assertEqual(validate_audit_changes("collector_availability_changed", changes), changes)

    def test_empty_evidence_is_accepted(self):
        self.assertEqual(validate_audit_changes("account_created", {}), {})

    def test_credentials_coordinates_and_full_payloads_are_rejected(self):
        for changes in [{"password": "secret"}, {"position": [120, 14]}, {"status": {"payload": "sensitive"}}, {"status": "x" * 101}]:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                validate_audit_changes("account_created", changes)

    def test_unsupported_actions_are_rejected(self):
        with self.assertRaises(ValidationError):
            validate_audit_changes("unknown", {})

    def test_contact_attempt_recorded_accepts_only_status_kind_and_outcome(self):
        changes = {"status": "submitted", "kind": "phone", "outcome": "contacted"}
        self.assertEqual(validate_audit_changes("contact_attempt_recorded", changes), changes)
        self.assertEqual(validate_audit_changes("contact_attempt_recorded", {}), {})

    def test_contact_attempt_recorded_rejects_unapproved_fields(self):
        for changes in [{"notes": "Called customer"}, {"position": [120, 14]}, {"unapproved": "field"}]:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                validate_audit_changes("contact_attempt_recorded", changes)

    def test_finance_audit_actions_accept_approved_fields(self):
        cases = [
            ("earning_rule_published", {"category": "address", "version": 1, "amount": "150.00"}),
            ("commission_created", {"category": "phone", "amount": "50.00", "status": "pending"}),
            ("commission_approved", {"status": "approved"}),
            ("commission_rejected", {"status": "rejected", "reason": "Duplicate"}),
            ("payout_created", {"status": "draft", "item_count": 2, "amount": "200.00"}),
            ("payout_approved", {"status": "approved"}),
            ("payout_paid", {"status": "paid", "reference": "REF123"}),
        ]
        for action, changes in cases:
            with self.subTest(action=action):
                self.assertEqual(validate_audit_changes(action, changes), changes)
                self.assertEqual(validate_audit_changes(action, {}), {})

    def test_finance_audit_actions_reject_unapproved_fields(self):
        cases = [
            ("earning_rule_published", {"extra": "field"}),
            ("commission_created", {"collector_id": 1}),
            ("commission_approved", {"notes": "ok"}),
            ("commission_rejected", {"unapproved": True}),
            ("payout_created", {"recipient": "Alice"}),
            ("payout_approved", {"amount": "100.00"}),
            ("payout_paid", {"bank_account": "12345"}),
        ]
        for action, changes in cases:
            with self.subTest(action=action):
                with self.assertRaises(ValidationError):
                    validate_audit_changes(action, changes)

    def test_point_audit_actions_accept_approved_fields(self):
        cases = [
            ("point_rule_published", {"category": "address", "version": 1, "points": 1}),
            ("point_awarded", {"category": "email", "points": 1}),
        ]
        for action, changes in cases:
            with self.subTest(action=action):
                self.assertEqual(validate_audit_changes(action, changes), changes)
                self.assertEqual(validate_audit_changes(action, {}), {})

    def test_point_audit_actions_reject_unapproved_fields(self):
        cases = [
            ("point_rule_published", {"extra": "field"}),
            ("point_rule_published", {"amount": "100.00"}),
            ("point_awarded", {"collector_id": 1}),
            ("point_awarded", {"status": "awarded"}),
        ]
        for action, changes in cases:
            with self.subTest(action=action):
                with self.assertRaises(ValidationError):
                    validate_audit_changes(action, changes)


    def test_system_admin_audit_actions_accept_approved_fields(self):
        cases = [
            ("system_admin_user_created", {"is_active": True, "collector_profile_created": False, "role_group_count": 2}),
            ("system_admin_user_created", {"is_active": False, "collector_profile_created": True}),
            ("system_admin_user_updated", {"is_active": True, "backoffice_permission_count": 3, "active_client_access_count": 1, "role_group_count": 2}),
            ("system_admin_user_updated", {"is_active": {"before": True, "after": False}}),
            ("system_admin_user_updated", {"backoffice_permission_count": {"before": 2, "after": 5}}),
            ("system_admin_user_updated", {"active_client_access_count": {"before": 1, "after": 2}}),
            ("system_admin_user_updated", {"role_group_count": {"before": 1, "after": 2}}),
            ("system_admin_user_updated", {"collector_profile_created": True}),
            ("system_admin_password_reset", {"password_reset": True}),
            ("system_admin_collector_status_changed", {"approval_status": "approved"}),
            ("system_admin_collector_status_changed", {"approval_status": {"before": "pending", "after": "approved"}}),
            ("system_admin_collector_converted", {"collector_status": "suspended", "role_group_count": 2}),
            ("system_admin_client_created", {"code": "CLI-01", "is_active": True}),
            ("system_admin_client_updated", {"code": "CLI-02", "is_active": False}),
            ("system_admin_client_updated", {"name_changed": True}),
            ("system_admin_client_updated", {"code": {"before": "CLI-01", "after": "CLI-02"}, "is_active": {"before": True, "after": False}, "name_changed": True}),
        ]
        for action, changes in cases:
            with self.subTest(action=action, changes=changes):
                self.assertEqual(validate_audit_changes(action, changes), changes)
                self.assertEqual(validate_audit_changes(action, {}), {})

    def test_system_admin_audit_actions_reject_unapproved_fields(self):
        cases = [
            ("system_admin_user_created", {"username": "newuser"}),
            ("system_admin_user_created", {"email": "new@example.com"}),
            ("system_admin_user_created", {"password": "secret"}),
            ("system_admin_user_created", {"first_name": "Alice"}),
            ("system_admin_user_updated", {"password": "secret"}),
            ("system_admin_user_updated", {"username": "admin"}),
            ("system_admin_password_reset", {"password": "plaintextpassword"}),
            ("system_admin_password_reset", {"user_id": 1}),
            ("system_admin_collector_status_changed", {"rating": 5}),
            ("system_admin_collector_status_changed", {"password": "secret"}),
            ("system_admin_collector_converted", {"password": "secret"}),
            ("system_admin_collector_converted", {"username": "admin"}),
            ("system_admin_client_created", {"name": "Client Name"}),
            ("system_admin_client_created", {"matching_strategy": "manual"}),
            ("system_admin_client_updated", {"name": "Client Name"}),
            ("system_admin_client_updated", {"policy_version": 2}),
        ]
        for action, changes in cases:
            with self.subTest(action=action, changes=changes):
                with self.assertRaises(ValidationError):
                    validate_audit_changes(action, changes)

    def test_collector_profile_created_boolean_type_enforcement(self):
        actions = ["system_admin_user_created", "system_admin_user_updated"]
        for action in actions:
            with self.subTest(action=action, val=True):
                self.assertEqual(
                    validate_audit_changes(action, {"collector_profile_created": True}),
                    {"collector_profile_created": True},
                )
            with self.subTest(action=action, val=False):
                self.assertEqual(
                    validate_audit_changes(action, {"collector_profile_created": False}),
                    {"collector_profile_created": False},
                )

            invalid_values = [
                "yes",
                1,
                0,
                None,
                [True],
                {"before": False, "after": True},
            ]
            for invalid in invalid_values:
                with self.subTest(action=action, invalid=invalid):
                    with self.assertRaises(ValidationError):
                        validate_audit_changes(action, {"collector_profile_created": invalid})


class AuditPersistenceTests(TestCase):
    def test_append_empty_redacted_evidence(self):
        actor = get_user_model().objects.create_user(username="audit-reviewer")
        event = append_audit_event(actor=actor, action="collector_availability_changed", entity_type="CollectorProfile", entity_id=1)
        self.assertEqual(event.changes, {})
        self.assertEqual(AuditEvent.objects.count(), 1)

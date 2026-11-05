"""Shared, Django-free audit presentation. Never modifies recorded evidence."""
import json


# label, sentence, icon, color; dashboard and audit log share this source.
ACTIONS = {
    'system_admin_user_created': ('User created', '{actor} created a user account', '+', 'blue'),
    'system_admin_user_updated': ('User updated', '{actor} updated a user account', '↻', 'amber'),
    'system_admin_client_created': ('Client created', '{actor} created a client', '+', 'blue'),
    'system_admin_client_updated': ('Client updated', '{actor} updated a client', '↻', 'amber'),
    'system_admin_collector_status_changed': ('Collector status changed', '{actor} changed a collector’s status', '↻', 'amber'),
    'system_admin_password_reset': ('Password reset', '{actor} reset a password', '↻', 'amber'),
    'system_admin_collector_converted': ('Collector converted to Back Office', '{actor} converted a collector to Back Office', '↻', 'amber'),
    'collector_invitation_sent': ('Invitation sent', '{actor} sent an invitation', '↗', 'blue'),
    'collector_invitation_resent': ('Invitation resent', '{actor} resent an invitation', '↗', 'blue'),
    'collector_invitation_accepted': ('Invitation accepted', '{actor} activated their account', '✓', 'green'),
    'collector_invitation_revoked': ('Invitation revoked', '{actor} revoked an invitation', '×', 'red'),
    'backoffice_invitation_sent': ('Back Office invitation sent', '{actor} sent a Back Office invitation', '↗', 'blue'),
    'backoffice_invitation_accepted': ('Back Office invitation accepted', '{actor} activated their Back Office account', '✓', 'green'),
    'backoffice_module_access_changed': ('Back Office access changed', '{actor} changed Back Office module access', '↻', 'amber'),
    'collector_email_changed': ('Collector email changed', '{actor} changed a collector’s email', '↻', 'amber'),
    'collector_suspended': ('Collector suspended', '{actor} suspended a collector', '!', 'amber'),
    'collector_reinstated': ('Collector reinstated', '{actor} reinstated a collector', '✓', 'green'),
    'collector_offboarded': ('Collector offboarded', '{actor} offboarded a collector', '×', 'red'),
    'collector_warning_issued': ('Collector warning issued', '{actor} issued a collector warning', '!', 'amber'),
    'collector_password_reset_sent': ('Collector password reset sent', '{actor} sent a collector password reset', '↗', 'blue'),
    'commission_approved': ('Commission approved', '{actor} approved a commission', '✓', 'green'),
    'commission_rejected': ('Commission rejected', '{actor} rejected a commission', '×', 'red'),
    'payment_verified': ('Payment verified', '{actor} verified a payment', '✓', 'green'),
    'payment_rejected': ('Payment rejected', '{actor} rejected a payment', '×', 'red'),
    'payout_created': ('Payout created', '{actor} created a payout', '+', 'blue'),
    'payout_approved': ('Payout approved', '{actor} approved a payout', '✓', 'green'),
    'payout_paid': ('Payout paid', '{actor} recorded a payout as paid', '✓', 'green'),
}


def action_presentation(code):
    fallback = code.replace('_', ' ').strip().capitalize() or 'Unknown action'
    label, sentence, icon, color = ACTIONS.get(code, (fallback, '{actor}: ' + fallback, '•', 'gray'))
    return dict(label=label, sentence=sentence, icon=icon, color=color)


def humanize_changes(changes):
    def display(value):
        if value is None:
            return 'None'
        if isinstance(value, bool):
            return 'Yes' if value else 'No'
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False, default=str)
        else:
            text = str(value)
        return text if len(text) <= 160 else text[:157] + '...'

    def tone(value):
        return {'pending': 'amber', 'rejected': 'red', 'approved': 'green'}.get(value, 'gray') if isinstance(value, str) else 'gray'

    chips = []
    for key, value in (changes.items() if isinstance(changes, dict) else []):
        pair = isinstance(value, dict) and 'before' in value and 'after' in value
        before, after = (value['before'], value['after']) if pair else (None, value)
        chips.append(dict(label=str(key).replace('_', ' ').capitalize(), pair=pair,
                          before=display(before), after=display(after),
                          before_color=tone(before), after_color=tone(after)))
    return chips

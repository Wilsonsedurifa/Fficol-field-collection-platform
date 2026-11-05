from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from apps.accounts.models import Client
from apps.user.models import BACK_OFFICE_PERMISSIONS, CollectorProfile
from apps.user.roles import get_backoffice_groups_queryset

User = get_user_model()


def get_available_backoffice_permissions():
    """Retrieve existing Permission records in the database matching BACK_OFFICE_PERMISSIONS."""
    perms = []
    for perm_str in BACK_OFFICE_PERMISSIONS:
        app_label, codename = perm_str.split(".", 1)
        p = Permission.objects.filter(
            content_type__app_label=app_label, codename=codename
        ).first()
        if p:
            perms.append(p)
    return perms


class CollectorCreateForm(forms.Form):
    first_name = forms.CharField(
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs={"class": "sa-input", "placeholder": "First name (optional)"}),
    )
    last_name = forms.CharField(
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs={"class": "sa-input", "placeholder": "Last name (optional)"}),
    )
    email = forms.EmailField(
        max_length=150,
        required=True,
        widget=forms.EmailInput(attrs={"class": "sa-input", "placeholder": "collector@example.com"}),
    )

    def clean_email(self):
        email = self.cleaned_data.get("email", "").strip().lower()
        if not email:
            raise ValidationError("Email address is required.")
        try:
            validate_email(email)
        except ValidationError:
            raise ValidationError("Enter a valid email address.")
        if User.objects.filter(email__iexact=email).exists() or User.objects.filter(username__iexact=email).exists():
            raise ValidationError("An active account with this email address already exists.")
        return email


class CollectorSetPasswordForm(forms.Form):
    password = forms.CharField(
        required=True,
        label="New Password",
        widget=forms.PasswordInput(
            attrs={"class": "sa-input", "placeholder": "Enter your new password", "autocomplete": "new-password"}
        ),
    )
    password_confirm = forms.CharField(
        required=True,
        label="Confirm New Password",
        widget=forms.PasswordInput(
            attrs={"class": "sa-input", "placeholder": "Re-enter your new password", "autocomplete": "new-password"}
        ),
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        password_confirm = cleaned_data.get("password_confirm")

        if password and password_confirm:
            if password != password_confirm:
                self.add_error("password_confirm", "Passwords do not match. Please re-enter them.")
            else:
                try:
                    validate_password(password, user=self.user)
                except ValidationError as e:
                    self.add_error("password", e)
        return cleaned_data



class BackOfficeUserCreateForm(forms.Form):
    modules = forms.MultipleChoiceField(required=True, widget=forms.CheckboxSelectMultiple, label="Back Office modules")
    username = forms.CharField(
        max_length=150,
        required=True,
        widget=forms.TextInput(
            attrs={"class": "sa-input", "placeholder": "Enter username", "autocomplete": "off"}
        ),
    )
    first_name = forms.CharField(
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs={"class": "sa-input", "placeholder": "First name (optional)"}),
    )
    last_name = forms.CharField(
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs={"class": "sa-input", "placeholder": "Last name (optional)"}),
    )
    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(attrs={"class": "sa-input", "placeholder": "staff@example.com"}),
    )
    role_groups = forms.MultipleChoiceField(
        required=True,
        widget=forms.CheckboxSelectMultiple(attrs={"class": "sa-checkbox-item"}),
        label="Back Office Role Groups",
        help_text="Select one or more role templates for this user.",
    )
    client_accesses = forms.MultipleChoiceField(
        required=False,
        widget=forms.CheckboxSelectMultiple(attrs={"class": "sa-checkbox-item"}),
        label="Active Client Access Scope",
        help_text="Select clients this Back Office user is authorized to manage.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.user.backoffice_modules import PERMISSION_GROUPS
        self.fields["modules"].choices = [(item["key"], item["label"]) for item in PERMISSION_GROUPS]
        self.fields["role_groups"].required = False

        groups = get_backoffice_groups_queryset()
        self.fields["role_groups"].choices = [(str(g.pk), g.name) for g in groups]
        active_clients = Client.objects.filter(is_active=True).order_by("name")
        self.fields["client_accesses"].choices = [
            (str(c.pk), f"{c.name} ({c.code})") for c in active_clients
        ]

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        if not username:
            raise ValidationError("Username is required.")
        if User.objects.filter(username__iexact=username).exists():
            raise ValidationError("A user with this username already exists.")
        return username

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError("An account with this email address already exists.")
        return email


class CollectorEditForm(forms.Form):
    first_name = forms.CharField(
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs={"class": "sa-input", "placeholder": "First name", "form": "collector-edit-form"}),
        label="First Name",
    )
    last_name = forms.CharField(
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs={"class": "sa-input", "placeholder": "Last name", "form": "collector-edit-form"}),
        label="Last Name",
    )
    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(attrs={"class": "sa-input", "placeholder": "collector@example.com", "form": "collector-edit-form"}),
        label="Email Address",
    )
    client_accesses = forms.MultipleChoiceField(
        required=False,
        widget=forms.CheckboxSelectMultiple(attrs={"class": "sa-checkbox-item", "form": "collector-edit-form"}),
        label="Active Client Access Scope",
    )

    def __init__(self, *args, target_user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.target_user = target_user
        active_clients = Client.objects.filter(is_active=True).order_by("name")
        self.fields["client_accesses"].choices = [
            (str(c.pk), f"{c.name} ({c.code})") for c in active_clients
        ]

    def clean_email(self):
        email = self.cleaned_data.get("email", "").strip().lower()
        if not email:
            raise ValidationError("Email address is required.")
        try:
            validate_email(email)
        except ValidationError:
            raise ValidationError("Enter a valid email address.")
        qs = User.objects.filter(email__iexact=email, is_active=True)
        if self.target_user:
            qs = qs.exclude(pk=self.target_user.pk)
        if qs.exists():
            raise ValidationError("An active account with this email address already exists.")
        return email


class CollectorSuspendForm(forms.Form):
    reason = forms.CharField(
        required=True,
        widget=forms.Textarea(
            attrs={"class": "sa-textarea", "rows": 3, "placeholder": "Enter detailed reason for suspension..."}
        ),
        label="Suspension Reason",
    )

    def clean_reason(self):
        reason = self.cleaned_data.get("reason", "").strip()
        if not reason:
            raise ValidationError("A reason is required to suspend a collector.")
        return reason


class CollectorReinstateForm(forms.Form):
    note = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={"class": "sa-textarea", "rows": 3, "placeholder": "Optional reinstatement note..."}
        ),
        label="Reinstatement Note",
    )


class CollectorOffboardForm(forms.Form):
    reason = forms.CharField(
        required=True,
        widget=forms.Textarea(
            attrs={"class": "sa-textarea", "rows": 3, "placeholder": "Enter detailed reason for offboarding..."}
        ),
        label="Offboarding Reason",
    )

    def clean_reason(self):
        reason = self.cleaned_data.get("reason", "").strip()
        if not reason:
            raise ValidationError("A reason is required to offboard a collector.")
        return reason


class CollectorWarningForm(forms.Form):
    level = forms.ChoiceField(
        choices=[("warning", "Warning"), ("final_warning", "Final Warning")],
        widget=forms.Select(attrs={"class": "sa-select"}),
        label="Warning Level",
    )
    reason = forms.CharField(
        required=True,
        widget=forms.Textarea(
            attrs={"class": "sa-textarea", "rows": 3, "placeholder": "Enter details/reason for this disciplinary warning..."}
        ),
        label="Warning Reason",
    )

    def clean_reason(self):
        reason = self.cleaned_data.get("reason", "").strip()
        if not reason:
            raise ValidationError("A reason is required to issue a warning.")
        return reason



class BackOfficeUserEditForm(forms.Form):
    modules = forms.MultipleChoiceField(required=True, widget=forms.CheckboxSelectMultiple, label="Back Office modules")
    is_active = forms.BooleanField(
        required=False,
        label="Active account (uncheck to deactivate login access)",
    )
    role_groups = forms.MultipleChoiceField(
        required=True,
        widget=forms.CheckboxSelectMultiple(attrs={"class": "sa-checkbox-item"}),
        label="Back Office Role Groups",
        help_text="Select one or more role templates for this user.",
    )
    backoffice_permissions = forms.MultipleChoiceField(
        required=False,
        widget=forms.CheckboxSelectMultiple(attrs={"class": "sa-checkbox-item"}),
        label="Exceptional Direct Permissions",
        help_text="Direct permissions are for exceptional overrides only. Normal roles are managed via Role Groups above.",
    )
    client_accesses = forms.MultipleChoiceField(
        required=False,
        widget=forms.CheckboxSelectMultiple(attrs={"class": "sa-checkbox-item"}),
        label="Active Client Access Scope",
    )
    new_password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(
            attrs={"class": "sa-input", "placeholder": "New password (leave blank to keep current)", "autocomplete": "new-password"}
        ),
        label="Reset Password",
    )
    new_password_confirm = forms.CharField(
        required=False,
        widget=forms.PasswordInput(
            attrs={"class": "sa-input", "placeholder": "Confirm new password", "autocomplete": "new-password"}
        ),
        label="Confirm New Password",
    )

    def __init__(self, *args, target_user=None, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.user.backoffice_modules import PERMISSION_GROUPS
        self.fields["modules"].choices = [(item["key"], item["label"]) for item in PERMISSION_GROUPS]
        self.fields["role_groups"].required = False

        self.target_user = target_user
        self.fields["backoffice_permissions"].required = False

        groups = get_backoffice_groups_queryset()
        self.fields["role_groups"].choices = [(str(g.pk), g.name) for g in groups]

        available_bo_perms = get_available_backoffice_permissions()
        self.fields["backoffice_permissions"].choices = [
            (str(p.pk), f"{p.name} [{p.content_type.app_label}.{p.codename}]")
            for p in available_bo_perms
        ]

        active_clients = Client.objects.filter(is_active=True).order_by("name")
        self.fields["client_accesses"].choices = [
            (str(c.pk), f"{c.name} ({c.code})") for c in active_clients
        ]

    def clean(self):
        cleaned_data = super().clean()
        new_password = cleaned_data.get("new_password")
        new_password_confirm = cleaned_data.get("new_password_confirm")

        if new_password or new_password_confirm:
            if not new_password:
                self.add_error("new_password", "Please enter the new password.")
            elif not new_password_confirm:
                self.add_error("new_password_confirm", "Please confirm the new password.")
            elif new_password != new_password_confirm:
                self.add_error("new_password_confirm", "New passwords do not match.")
            else:
                try:
                    validate_password(new_password, user=self.target_user)
                except ValidationError as e:
                    self.add_error("new_password", e)

        # Prevention of dual active roles: Cannot assign role groups if target user has an active approved collector profile
        if self.target_user and hasattr(self.target_user, "collector_profile"):
            try:
                prof = self.target_user.collector_profile
                if prof and prof.approval_status == CollectorProfile.ApprovalStatus.APPROVED:
                    role_groups = cleaned_data.get("role_groups")
                    if role_groups or cleaned_data.get("modules"):
                        self.add_error(
                            "role_groups",
                            "This user currently has an approved Collector Profile. Please use the conversion workflow to suspend the collector role before assigning Back Office roles.",
                        )
            except CollectorProfile.DoesNotExist:
                pass

        return cleaned_data


class CollectorConversionForm(forms.Form):
    role_groups = forms.MultipleChoiceField(
        required=True,
        widget=forms.CheckboxSelectMultiple(attrs={"class": "sa-checkbox-item"}),
        label="Assign Back Office Role Groups",
        help_text="Select one or more Back Office role groups to assign.",
    )
    client_accesses = forms.MultipleChoiceField(
        required=False,
        widget=forms.CheckboxSelectMultiple(attrs={"class": "sa-checkbox-item"}),
        label="Active Client Access Scope",
    )
    confirm_conversion = forms.BooleanField(
        required=True,
        label="I confirm that the collector profile will be suspended and historical records preserved.",
    )

    def __init__(self, *args, target_user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.target_user = target_user

        groups = get_backoffice_groups_queryset()
        self.fields["role_groups"].choices = [(str(g.pk), g.name) for g in groups]

        active_clients = Client.objects.filter(is_active=True).order_by("name")
        self.fields["client_accesses"].choices = [
            (str(c.pk), f"{c.name} ({c.code})") for c in active_clients
        ]


# Backward compatibility forms
class UserCreateForm(forms.Form):
    username = forms.CharField(
        max_length=150,
        required=True,
        widget=forms.TextInput(
            attrs={"class": "sa-input", "placeholder": "Enter username", "autocomplete": "off"}
        ),
    )
    password = forms.CharField(
        required=True,
        widget=forms.PasswordInput(
            attrs={"class": "sa-input", "placeholder": "Temporary password", "autocomplete": "new-password"}
        ),
    )
    password_confirm = forms.CharField(
        required=True,
        label="Confirm temporary password",
        widget=forms.PasswordInput(
            attrs={"class": "sa-input", "placeholder": "Confirm temporary password", "autocomplete": "new-password"}
        ),
    )
    first_name = forms.CharField(
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs={"class": "sa-input", "placeholder": "First name (optional)"}),
    )
    last_name = forms.CharField(
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs={"class": "sa-input", "placeholder": "Last name (optional)"}),
    )
    email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(attrs={"class": "sa-input", "placeholder": "email@example.com (optional)"}),
    )
    create_collector_profile = forms.BooleanField(
        required=False,
        initial=False,
        label="Create pending Collector Profile",
    )

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        if not username:
            raise ValidationError("Username is required.")
        if User.objects.filter(username__iexact=username).exists():
            raise ValidationError("A user with this username already exists.")
        return username

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        password_confirm = cleaned_data.get("password_confirm")

        if password and password_confirm:
            if password != password_confirm:
                self.add_error("password_confirm", "Passwords do not match.")
            else:
                candidate_user = User(
                    username=cleaned_data.get("username") or "",
                    first_name=cleaned_data.get("first_name") or "",
                    last_name=cleaned_data.get("last_name") or "",
                    email=cleaned_data.get("email") or "",
                )
                try:
                    validate_password(password, user=candidate_user)
                except ValidationError as e:
                    self.add_error("password", e)

        return cleaned_data


class UserEditForm(forms.Form):
    is_active = forms.BooleanField(
        required=False,
        label="Active account",
    )
    client_accesses = forms.MultipleChoiceField(
        required=False,
        widget=forms.CheckboxSelectMultiple(attrs={"class": "sa-checkbox-item"}),
        label="Active Client Access",
    )
    backoffice_permissions = forms.MultipleChoiceField(
        required=False,
        widget=forms.CheckboxSelectMultiple(attrs={"class": "sa-checkbox-item"}),
        label="Direct Back Office Capabilities",
    )
    collector_status = forms.ChoiceField(
        required=False,
        choices=CollectorProfile.ApprovalStatus.choices,
        widget=forms.Select(attrs={"class": "sa-select"}),
        label="Collector Approval Status",
    )
    create_collector_profile = forms.BooleanField(
        required=False,
        label="Create pending Collector Profile",
    )
    new_password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(
            attrs={"class": "sa-input", "placeholder": "New password (leave blank to keep current)", "autocomplete": "new-password"}
        ),
        label="Reset Password",
    )
    new_password_confirm = forms.CharField(
        required=False,
        widget=forms.PasswordInput(
            attrs={"class": "sa-input", "placeholder": "Confirm new password", "autocomplete": "new-password"}
        ),
        label="Confirm New Password",
    )

    def __init__(self, *args, target_user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.target_user = target_user

        # Populate active client choices
        active_clients = Client.objects.filter(is_active=True).order_by("name")
        self.fields["client_accesses"].choices = [
            (str(c.pk), f"{c.name} ({c.code})") for c in active_clients
        ]

        # Populate direct Back Office permission choices
        available_bo_perms = get_available_backoffice_permissions()
        self.fields["backoffice_permissions"].choices = [
            (str(p.pk), f"{p.name} [{p.content_type.app_label}.{p.codename}]")
            for p in available_bo_perms
        ]

        has_profile = False
        if target_user and hasattr(target_user, "collector_profile"):
            try:
                has_profile = target_user.collector_profile is not None
            except CollectorProfile.DoesNotExist:
                has_profile = False

        if has_profile:
            self.fields.pop("create_collector_profile", None)
        else:
            self.fields.pop("collector_status", None)

    def clean(self):
        cleaned_data = super().clean()
        new_password = cleaned_data.get("new_password")
        new_password_confirm = cleaned_data.get("new_password_confirm")

        if new_password or new_password_confirm:
            if not new_password:
                self.add_error("new_password", "Please enter the new password.")
            elif not new_password_confirm:
                self.add_error("new_password_confirm", "Please confirm the new password.")
            elif new_password != new_password_confirm:
                self.add_error("new_password_confirm", "New passwords do not match.")
            else:
                try:
                    validate_password(new_password, user=self.target_user)
                except ValidationError as e:
                    self.add_error("new_password", e)

        return cleaned_data


class ClientForm(forms.ModelForm):
    class Meta:
        model = Client
        fields = ["code", "name", "is_active"]
        widgets = {
            "code": forms.TextInput(attrs={"class": "sa-input", "placeholder": "Client code (e.g. CLI-001)"}),
            "name": forms.TextInput(attrs={"class": "sa-input", "placeholder": "Client organization name"}),
            "is_active": forms.CheckboxInput(attrs={"class": "sa-checkbox"}),
        }

    def clean_code(self):
        code = self.cleaned_data["code"].strip()
        if not code:
            raise ValidationError("Client code is required.")
        qs = Client.objects.filter(code__iexact=code)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError("A client with this code already exists.")
        return code

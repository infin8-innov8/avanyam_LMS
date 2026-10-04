from __future__ import annotations

from django import forms
from django.contrib import admin
from django.contrib.auth import password_validation
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.core.exceptions import ValidationError

from apps.accounts.models import Role, RoleAssignment, SignupRequest, User


class UserCreationForm(forms.ModelForm):
    """Admin "add user" form.

    Django's own ``UserCreationForm`` is unusable here: it is bound to
    ``django.contrib.auth.models.User`` and declares ``Meta.fields =
    ("username",)``, a field this project's user model does not have. Django's
    ``UserAdmin`` also hardcodes ``add_fieldsets`` mentioning ``username``, so
    inheriting either one makes ``/admin/accounts/user/add/`` raise
    ``FieldError``. Both are replaced below.
    """

    password1 = forms.CharField(label="Password", widget=forms.PasswordInput, strip=False)
    password2 = forms.CharField(label="Password confirmation", widget=forms.PasswordInput, strip=False)

    class Meta:
        model = User
        fields = ("email", "full_name")

    def clean_password2(self) -> str:
        password1 = self.cleaned_data.get("password1")
        password2 = self.cleaned_data.get("password2")
        if password1 and password2 and password1 != password2:
            raise ValidationError("The two password fields didn't match.")
        return password2

    def _post_clean(self) -> None:
        super()._post_clean()
        password = self.cleaned_data.get("password2")
        if password:
            try:
                password_validation.validate_password(password, self.instance)
            except ValidationError as error:
                self.add_error("password2", error)

    def save(self, commit: bool = True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password1"])
        if commit:
            user.save()
        return user


class UserChangeForm(forms.ModelForm):
    """Admin "change user" form.

    Django's ``UserChangeForm`` sets ``Meta.fields = "__all__"``, which also
    pulls in its own password help-text handling for a field layout that does not
    apply here.
    """

    class Meta:
        model = User
        fields = "__all__"


class RoleInline(admin.TabularInline):
    model = RoleAssignment
    extra = 0
    # RoleAssignment has TWO FKs to User (user, assigned_by), so admin.E202
    # requires the disambiguation.
    fk_name = "user"
    autocomplete_fields = ["role", "assigned_by"]


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    form = UserChangeForm
    add_form = UserCreationForm
    inlines = [RoleInline]
    list_display = ("email", "full_name", "auth_source", "approval_status", "is_trainer")
    list_filter = ("approval_status", "auth_source", "is_active", "role_assignments__role__slug")
    search_fields = ("email", "full_name", "oidc_subject", "ldap_dn")
    readonly_fields = (
        "id",
        "created_at",
        "updated_at",
        "last_login",
        "selected_trainer",
    )
    ordering = ("full_name",)
    filter_horizontal = ("groups", "user_permissions")

    # Django's UserAdmin.add_fieldsets hardcodes "username", which this model
    # does not have. ModelAdmin.get_fieldsets() returns add_fieldsets (not
    # fieldsets) when obj is None, so the add view 500s unless this is replaced.
    add_fieldsets = (
        (None, {"classes": ("wide",), "fields": ("email", "full_name", "password1", "password2")}),
    )

    fieldsets = (
        (None, {"fields": ("id", "email", "full_name", "password")}),
        ("Identity source", {"fields": ("auth_source", "oidc_subject", "ldap_dn")}),
        # `selected_trainer` is a deprecated column kept only so the FK that
        # production data points at still resolves (D42). It is readonly, not
        # just hidden: a superuser editing it in /admin/ would be writing the one
        # field the application deliberately ignores, which is exactly the sort
        # of drift the deprecation is meant to prevent. It stays listed so an
        # operator can see historical values during a migration.
        (
            "Approval",
            {
                "fields": ("approval_status", "decided_by", "decided_at"),
                "description": "",
            },
        ),
        # `must_change_password` was removed from the model (D43); no placeholder
        # for it remains in this fieldset.
        ("Security", {"fields": ("is_active", "is_staff", "is_superuser")}),
        (
            "Timestamps",
            {"fields": ("last_login", "date_joined", "created_at", "updated_at"), "classes": ("collapse",)},
        ),
        (
            "Permissions",
            {"classes": ("collapse",), "fields": ("groups", "user_permissions")},
        ),
    )

    # `role_assignments` is an inline, but the list filter needs it walkable.
    def is_trainer(self, obj: User) -> bool:
        return obj.is_trainer

    is_trainer.boolean = True
    is_trainer.short_description = "trainer"


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ("slug", "name", "created_at")
    search_fields = ("slug", "name")


@admin.register(SignupRequest)
class SignupRequestAdmin(admin.ModelAdmin):
    list_display = (
        "full_name",
        "email",
        "requested_role",
        "status",
        "created_at",
        "decided_at",
    )
    list_filter = ("status", "requested_role")
    search_fields = ("full_name", "email")
    # `created_by` is writable here deliberately: it is a record of who filled
    # the form in, and correcting a mis-entered value is an ordinary data fix.
    # `selected_trainer` is readonly for the same reason as on User -- it is
    # deprecated and the application ignores it (D42).
    readonly_fields = ("id", "created_at", "updated_at", "selected_trainer")
    autocomplete_fields = ("decided_by", "user", "created_by")

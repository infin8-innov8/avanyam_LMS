from __future__ import annotations

from django import forms
from django.contrib.auth.forms import AuthenticationForm

from apps.accounts.domain.enums import RequestedRole
from apps.accounts.models import User
from apps.accounts.service.signup import SignupError, submit_application


class RoleChoiceField(forms.ChoiceField):
    """The roles an applicant may ask for -- `trainee` or `trainer`.

    The choices are generated from `RequestedRole`, an enum that has **no**
    `ADMIN` member. That is the point: `prd.md` §4 requires admin to be
    structurally unreachable from signup, and a hand-written `choices=` list is
    one edit away from offering it. Generating from the enum means a future role
    is unreachable until somebody adds it there on purpose, and `SignupForm`'s
    validator refuses it a second time at the service boundary.

    Also used by the Admin's approval queue, where the same enum is the set of
    roles an override may choose.
    """

    def __init__(self, **kwargs):
        kwargs.setdefault(
            "choices",
            [(role.value, role.value.capitalize()) for role in RequestedRole],
        )
        kwargs.setdefault("label", "I am applying as")
        kwargs.setdefault(
            "help_text",
            "An admin reviews every registration and can change this before "
            "approving it.",
        )
        super().__init__(**kwargs)


class SignupForm(forms.Form):
    """Registration. Submission creates a *pending* account, never an active one.

    Serves both entry points: a person registering themselves, and a Trainer or
    Admin creating an account for someone else. The fields are identical because
    the outcomes are identical -- a pending account plus an application -- and the
    only difference is who fills the form in, which the service records.
    """

    full_name = forms.CharField(
        max_length=255,
        widget=forms.TextInput(attrs={"autocomplete": "name", "autofocus": True}),
    )
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={"autocomplete": "email", "inputmode": "email"})
    )
    requested_role = RoleChoiceField()
    password1 = forms.CharField(
        label="Password",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    password2 = forms.CharField(
        label="Confirm password",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    def clean_email(self) -> str:
        email = User.objects.normalize_email(self.cleaned_data["email"]).strip()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account already exists for that email address.")
        return email

    def clean_full_name(self) -> str:
        return " ".join(self.cleaned_data["full_name"].split())

    def _password_errors(self) -> None:
        p1, p2 = self.cleaned_data.get("password1"), self.cleaned_data.get("password2")
        if p1 and p2 and p1 != p2:
            raise forms.ValidationError("The two passwords do not match.")
        # Django's AUTH_PASSWORD_VALIDATORS run here, so the strength rules in
        # settings are the single source of truth for what a password may be.
        from django.contrib.auth.password_validation import validate_password

        validate_password(p1, user=self._password_validation_user())

    def _password_validation_user(self):
        email = self.cleaned_data.get("email") or ""
        name = self.cleaned_data.get("full_name") or ""
        return User(email=email, full_name=name)

    def clean(self):
        cleaned = super().clean()
        if self.cleaned_data.get("password1") and self.cleaned_data.get("password2"):
            self._password_errors()
        return cleaned

    def save(self, requester: User | None = None):
        try:
            return submit_application(
                email=self.cleaned_data["email"],
                full_name=self.cleaned_data["full_name"],
                password=self.cleaned_data["password1"],
                requested_role=self.cleaned_data["requested_role"],
                requester=requester,
            )
        except SignupError as exc:
            raise forms.ValidationError(str(exc)) from exc


class LoginForm(AuthenticationForm):
    """Project-branded login. Username field is labelled Email."""

    username = forms.EmailField(
        label="Email",
        widget=forms.EmailInput(
            attrs={"autocomplete": "email", "inputmode": "email", "autofocus": True}
        ),
    )

from __future__ import annotations

from django import forms
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm

from apps.accounts.models import User
from apps.accounts.service.signup import SignupError, submit_application
from apps.accounts.policies import is_approved


class TrainerChoiceField(forms.ModelChoiceField):
    """Only trainers who can actually approve are offered.

    Filtering in the queryset rather than validating later means a trainee
    cannot be told "that trainer is unavailable" after they have already filled
    in the rest of the form.
    """

    def __init__(self, **kwargs):
        from apps.accounts.models import ROLE_ADMIN, ROLE_TRAINER

        eligible = (
            User.objects.active()
            .filter(role_assignments__role__slug__in=[ROLE_TRAINER, ROLE_ADMIN])
            .distinct()
            .order_by("full_name")
        )
        kwargs.setdefault("queryset", eligible)
        kwargs.setdefault(
            "label",
            "Choose your trainer",
        )
        kwargs.setdefault(
            "help_text",
            "Your trainer reviews and approves your registration. "
            "Trainers are grouped like departments, so pick the one responsible for you.",
        )
        super().__init__(**kwargs)


class SignupForm(forms.Form):
    """Registration. Submission creates a *pending* account, never an active one."""

    full_name = forms.CharField(
        max_length=255,
        widget=forms.TextInput(attrs={"autocomplete": "name", "autofocus": True}),
    )
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={"autocomplete": "email", "inputmode": "email"})
    )
    trainer = TrainerChoiceField()
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
                selected_trainer=self.cleaned_data["trainer"],
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


class FirstPasswordChangeForm(PasswordChangeForm):
    """Seeded accounts arrive with a predictable password and must replace it."""

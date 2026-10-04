"""Transactional signup service.

Signup is the one flow that must not be half-applied: a User without its
SignupRequest breaks the §6.2 "rejected email cannot re-register" guarantee,
and a SignupRequest whose notification failed leaves a trainee waiting forever
with nobody told. So the DB writes are one transaction and the notification is
dispatched only after commit.

The password is never stored on SignupRequest -- only the hash on User.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.db import IntegrityError, transaction

from apps.accounts.domain.enums import ApprovalStatus, AuthSource
from apps.accounts.models import ROLE_TRAINEE, Role, RoleAssignment, SignupRequest, User
from apps.accounts.policies import is_approved


class SignupError(Exception):
    """Signup refused. `code` is stable; `message` is safe to show a user."""


@dataclass(frozen=True, slots=True)
class SignupResult:
    user: User
    request: SignupRequest


def signup_enabled() -> bool:
    """`ACCOUNTS.SIGNUP_ENABLED` kill switch (§15) -- no redeploy to flip it."""
    return bool(getattr(settings, "ACCOUNTS", {}).get("SIGNUP_ENABLED", False))


def _validate_trainer(trainer: User) -> None:
    if not is_approved(trainer):
        raise SignupError("That trainer is not available to approve applications.")
    if not trainer.has_role("trainer") and not trainer.has_role("admin"):
        raise SignupError("That trainer is not available to approve applications.")


def _normalise_full_name(raw: str) -> str:
    """Collapse runs of whitespace and trim.

    `SignupForm.clean_full_name` does this too, but the service is the boundary
    that owns the invariant, and `submit_application` is called from more than the
    form. Leaving it to the form means any other caller stores `"Jane  A  Smith"`
    and the duplicate-looking names show up as distinct rows in the trainer list.
    """
    return " ".join(raw.split())


def submit_application(
    *,
    email: str,
    full_name: str,
    password: str,
    selected_trainer: User,
    requester: User | None = None,
) -> SignupResult:
    """Register a trainee as *pending* and tell their chosen trainer.

    The trainee is created `pending`, never `approved`: signup exists and is
    inert until a trainer decides (§15).

    `must_change_password` is deliberately left at its default. It exists for
    accounts created with a *predictable* password -- the seeded staff accounts,
    whose password is derived from the person's name. A signup user chose their
    own password, so demanding they immediately replace it teaches them to ignore
    the prompt.
    """
    if not signup_enabled():
        raise SignupError("Registration is currently closed.")

    _validate_trainer(selected_trainer)

    if requester is not None and not is_approved(requester):
        raise SignupError("Only approved staff may submit applications on behalf of others.")

    email = User.objects.normalize_email(email).strip()
    if not email:
        raise SignupError("A valid email address is required.")

    full_name = _normalise_full_name(full_name)
    if not full_name:
        raise SignupError("A name is required.")

    if User.objects.filter(email__iexact=email).exists():
        raise SignupError("An account already exists for that email address.")

    # §6.2: the SignupRequest row is the durable blocklist. Check it explicitly
    # so a declined address gets an accurate message instead of a raw
    # IntegrityError from the unique constraint.
    #
    # `blocking_for_email` decides this, and it is now load-bearing rather than
    # merely tidy: `uniq_signup_email_open` excludes REDIRECTED rows, so the
    # database will happily accept a second application for a redirected address
    # and this check is the only thing stopping a *rejected* one.
    blocking = SignupRequest.blocking_for_email(email)
    if blocking is not None:
        if blocking.status == ApprovalStatus.REJECTED:
            raise SignupError(
                "That email address was declined previously and cannot register again. "
                "Please contact your trainer."
            )
        if blocking.status == ApprovalStatus.SUSPENDED:
            raise SignupError(
                "That account is suspended. Please contact your trainer."
            )
        raise SignupError("An application for that email address already exists.")

    # Nothing blocking. Any earlier rows for this address are REDIRECTED -- the
    # trainer declined but explicitly allowed another application, usually against
    # a different trainer. That is exactly the case this is here to permit.
    #
    # The old pending User was deleted when the redirect was recorded (see
    # `approval.decide`), so the check above sees no account and signup proceeds
    # as an ordinary first-time registration against the newly chosen trainer.

    try:
        with transaction.atomic():
            user = User.objects.create_user(
                email=email,
                password=password,
                full_name=full_name,
                auth_source=AuthSource.SIGNUP,
                approval_status=ApprovalStatus.PENDING,
                selected_trainer=selected_trainer,
            )
            trainee_role, _ = Role.objects.get_or_create(
                slug=ROLE_TRAINEE, defaults={"name": "Trainee"}
            )
            RoleAssignment.objects.create(user=user, role=trainee_role)
            request = SignupRequest.objects.create(
                email=email,
                full_name=full_name,
                selected_trainer=selected_trainer,
                status=ApprovalStatus.PENDING,
                user=user,
            )
    except IntegrityError as exc:  # lost a race against a concurrent signup
        raise SignupError("An application for that email address already exists.") from exc

    # Dispatch after commit: if the transaction rolls back we must not email a
    # trainer about a phantom application.
    transaction.on_commit(lambda: _notify_trainer(request.pk))

    return SignupResult(user=user, request=request)


def _notify_trainer(request_pk) -> None:
    """Import lazily to keep this module free of Celery at import time."""
    from apps.accounts.tasks import notify_trainer_of_application

    notify_trainer_of_application.delay(str(request_pk))

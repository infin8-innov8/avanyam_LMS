"""Transactional signup service.

Signup is the one flow that must not be half-applied: a User without its
SignupRequest breaks the §6.2 "rejected email cannot re-register" guarantee,
and a SignupRequest whose notification failed leaves an applicant waiting forever
with nobody told. So the DB writes are one transaction and the notification is
dispatched only after commit.

The password is never stored on SignupRequest -- only the hash on User.

**No role is granted here.** `rules.md` §6 forbids granting a role without an
approval decision, so a pending account carries no `RoleAssignment` at all. The
role arrives in `service.approval.decide`, and an Admin may substitute a different
one from the requested value. An earlier version assigned `trainee` at signup,
which meant a rejected applicant's account held a role the whole time.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.db import IntegrityError, transaction

from apps.accounts.domain.enums import (
    REQUESTABLE_ROLES,
    ApprovalStatus,
    AuthSource,
    RequestedRole,
)
from apps.accounts.models import SignupRequest, User
from apps.accounts.policies import can_register


class SignupError(Exception):
    """Signup refused. `code` is stable; `message` is safe to show a user."""


@dataclass(frozen=True, slots=True)
class SignupResult:
    user: User
    request: SignupRequest


def signup_enabled() -> bool:
    """`ACCOUNTS.SIGNUP_ENABLED` kill switch (§15) -- no redeploy to flip it."""
    return bool(getattr(settings, "ACCOUNTS", {}).get("SIGNUP_ENABLED", False))


def _validate_requested_role(raw: str) -> RequestedRole:
    """Refuse anything outside `RequestedRole`, and say so as a form error.

    The form already generates its choices from the same enum, so reaching this
    with a bad value means a direct POST or a stale client. `admin` lands here
    too, and is refused by construction rather than by the absence of a choice --
    `prd.md` §4 asks for a validator, not a hidden field.
    """
    try:
        role = RequestedRole(raw)
    except ValueError as exc:
        raise SignupError("That is not a role you can apply for.") from exc
    if role not in REQUESTABLE_ROLES:
        # Unreachable while `REQUESTABLE_ROLES` is built from the enum. Written
        # anyway so that widening the enum later cannot quietly widen signup.
        raise SignupError("That is not a role you can apply for.")
    return role


def _normalise_full_name(raw: str) -> str:
    """Collapse runs of whitespace and trim.

    `SignupForm.clean_full_name` does this too, but the service is the boundary
    that owns the invariant, and `submit_application` is called from more than the
    form. Leaving it to the form means any other caller stores `"Jane  A  Smith"`
    and the duplicate-looking names show up as distinct rows in the admin list.
    """
    return " ".join(raw.split())


def submit_application(
    *,
    email: str,
    full_name: str,
    password: str,
    requested_role: str,
    requester: User | None = None,
) -> SignupResult:
    """Register an applicant as *pending* and put the application in the Admin queue.

    Serves both callers: a person registering themselves (`requester=None`) and a
    Trainer or Admin creating an account for them (`requester` set). The resulting
    state is identical, and `created_by` is what distinguishes them later.

    The applicant is created `pending`, never `approved`, and with no role.
    """
    if not signup_enabled():
        raise SignupError("Registration is currently closed.")

    role = _validate_requested_role(requested_role)

    if requester is not None:
        verdict = can_register(requester)
        if not verdict.allowed:
            raise SignupError(verdict.reason)

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
    # `blocking_for_email` decides this, and it is load-bearing rather than
    # merely tidy: `uniq_signup_email_open` excludes REDIRECTED rows, so the
    # database will happily accept a second application for a redirected address
    # and this check is the only thing stopping a *rejected* one.
    blocking = SignupRequest.blocking_for_email(email)
    if blocking is not None:
        if blocking.status == ApprovalStatus.REJECTED:
            raise SignupError(
                "That email address was declined previously and cannot register "
                "again. Please contact an administrator."
            )
        if blocking.status == ApprovalStatus.SUSPENDED:
            raise SignupError(
                "That account is suspended. Please contact an administrator."
            )
        raise SignupError("An application for that email address already exists.")

    # Nothing blocking. Any earlier rows for this address are REDIRECTED -- the
    # admin declined but explicitly allowed another application. That is exactly
    # the case this is here to permit.

    try:
        with transaction.atomic():
            user = User.objects.create_user(
                email=email,
                password=password,
                full_name=full_name,
                auth_source=AuthSource.SIGNUP,
                approval_status=ApprovalStatus.PENDING,
                # D45: pending means inactive, so `ModelBackend` refuses the
                # credentials on every backend rather than relying on the login
                # view to notice. `User.move_to` flips this back on approval.
                is_active=False,
            )
            request = SignupRequest.objects.create(
                email=email,
                full_name=full_name,
                requested_role=role,
                created_by=requester,
                status=ApprovalStatus.PENDING,
                user=user,
            )
    except IntegrityError as exc:  # lost a race against a concurrent signup
        raise SignupError("An application for that email address already exists.") from exc

    # Dispatch after commit: if the transaction rolls back we must not email an
    # administrator about a phantom application.
    transaction.on_commit(lambda: _notify_admins(request.pk))

    return SignupResult(user=user, request=request)


def _notify_admins(request_pk) -> None:
    """Import lazily to keep this module free of Celery at import time."""
    from apps.accounts.tasks import notify_admins_of_application

    notify_admins_of_application.delay(str(request_pk))

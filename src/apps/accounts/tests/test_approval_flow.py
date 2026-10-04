"""Regression tests for defects found on 2026-10-04, plus the ones the
Admin-approval rewrite (D41--D45) made possible.

Each test names the bug it prevents. If one of these fails, the corresponding
defect is back.

Note on the rewrite. Sections 1, 2 and 4 originally passed a `trainer` as the
approver and a `selected_trainer` as the routing decision. Neither exists any
more: approval is an admin act in one shared queue, and an application carries a
`requested_role` instead. The defects those sections pinned were real and are
still pinned here -- only the vocabulary changed.

Section 3 is the exception. It pinned `MustChangePasswordMiddleware`, which D43
removed along with the flag it enforced. Its place is taken by
:func:`test_no_middleware_is_installed_for_a_removed_flag` and the
pending-login tests, which assert the replacement behaviour rather than
pretending the old gate still exists.
"""

from __future__ import annotations

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse

from apps.accounts.models import SignupRequest, User
from apps.accounts.service.approval import DecisionError, decide
from apps.accounts.service.signup import submit_application

from .conftest import GOOD_PASSWORD, roles_of

pytestmark = pytest.mark.django_db


def _apply(email="applicant@example.test", name="Applicant", role="trainee", **extra):
    """Submit an application the way the public form does."""
    return submit_application(
        email=email,
        full_name=name,
        password=GOOD_PASSWORD,
        requested_role=role,
        **extra,
    )


# ---------------------------------------------------------------------------
# 1. Approval grants the requested role, and only the requested role.
#
# approval.py used to branch on
#     if req.selected_trainer_id == approver.pk: grant ROLE_TRAINER
# That condition was true for every ordinary application, because the approver
# WAS the trainer the applicant picked. So a normal approval silently promoted
# the trainee to trainer -- they could then open the approval queue and approve
# other people. Verified against the running app before the fix.
#
# The rewrite replaced that branch with `requested_role`, so the same bug class
# has a new shape: the role now comes from a field the *applicant* filled in, and
# the only thing stopping a direct POST from asking for `trainer` is the
# `RequestedRole` enum. Both halves are pinned below.
# ---------------------------------------------------------------------------


def test_approving_a_trainee_request_grants_trainee_and_nothing_more(admin):
    result = _apply(role="trainee")
    # Signup grants nothing at all (D49), so this is empty before the decision.
    assert roles_of(result.user) == []

    decide(approver=admin, request_pk=result.request.pk, approve=True)

    result.user.refresh_from_db()
    assert result.user.approval_status == "approved"
    assert roles_of(result.user) == ["trainee"]
    assert result.user.is_trainer is False, (
        "approval must grant exactly what was asked for"
    )


def test_a_trainer_request_grants_trainer(admin):
    result = _apply(role="trainer")

    decide(approver=admin, request_pk=result.request.pk, approve=True)

    result.user.refresh_from_db()
    assert roles_of(result.user) == ["trainer"]
    assert result.user.is_trainer is True


def test_an_admin_may_override_the_requested_role_at_approval(admin):
    """The override is the feature D41 added, so it gets its own test.

    An applicant who asks for `trainee` and is actually a trainer's colleague
    should not need a declined application and a fresh one.
    """
    result = _apply(role="trainee")

    outcome = decide(
        approver=admin, request_pk=result.request.pk, approve=True, role="trainer"
    )

    assert outcome.granted_role == "trainer"
    assert roles_of(result.user) == ["trainer"]


def test_approval_cannot_mint_an_admin(admin):
    """D44: the role vocabulary an approval draws from has no admin member.

    Enforced twice on purpose. `RequestedRole` makes it unrepresentable, and the
    decision service re-checks so that a future caller cannot widen the enum's
    `REQUESTABLE_ROLES` tuple and quietly reopen this.
    """
    result = _apply(role="trainee")

    with pytest.raises(DecisionError):
        decide(
            approver=admin, request_pk=result.request.pk, approve=True, role="admin"
        )

    result.request.refresh_from_db()
    assert result.request.status == "pending"


def test_approved_trainee_cannot_approve_anyone(admin, approved_trainee):
    """The escalation is only dangerous if it grants real capability."""
    result = _apply(email="second.trainee@example.test", name="Second Trainee")

    with pytest.raises(DecisionError):
        decide(approver=approved_trainee, request_pk=result.request.pk, approve=True)

    result.request.refresh_from_db()
    assert result.request.status == "pending"


# ---------------------------------------------------------------------------
# 2. select_for_update() must not be joined to a nullable FK.
#
# SignupRequest.user and .selected_trainer are nullable, so select_related()
# builds an outer join and PostgreSQL raises
#     FOR UPDATE cannot be applied to the nullable side of an outer join
# Every approval 500'd on the real database.
# ---------------------------------------------------------------------------


def test_decide_works_on_postgres_with_nullable_joined_fks(admin):
    """This is the test that fails with the joined select_for_update."""
    result = _apply(email="lock.trainee@example.test", name="Lock Trainee")
    assert result.request.user_id is not None

    outcome = decide(approver=admin, request_pk=result.request.pk, approve=True)
    assert outcome.request.status == "approved"
    assert outcome.user.approval_status == "approved"
    assert outcome.request.decided_by_id == admin.pk


def test_decide_sets_auditing_fields(admin):
    result = _apply(email="audit.trainee@example.test", name="Audit Trainee")
    decide(
        approver=admin, request_pk=result.request.pk, approve=True, note="looks good"
    )

    result.request.refresh_from_db()
    assert result.request.decision_note == "looks good"
    assert result.request.decided_by_id == admin.pk
    assert result.request.decided_at is not None


# ---------------------------------------------------------------------------
# 3. The forced-password gate is gone, and nothing grew in its place.
#
# The middleware checked request.resolver_match, which is always None in the
# request phase (URL resolution has not run yet), so EXEMPT_NAMES never matched
# and logout bounced to the password page, leaving the session authenticated.
#
# D43 removed the whole mechanism: self-registrants choose their own password
# and a trainer creating an account sets one with them, so there was no
# predictable password left to contain. These tests assert the removal is real and
# that ordinary password change still works.
# ---------------------------------------------------------------------------


def test_no_middleware_is_installed_for_a_removed_flag():
    """If a class referencing a deleted model field were still installed, Django
    would import it at boot. Asserting the absence in the *setting* rather than
    just relying on the import failing keeps the failure legible."""
    assert not any(
        "accounts.middleware" in entry for entry in settings.MIDDLEWARE
    ), "MustChangePasswordMiddleware was removed in D43 and must not come back"


def test_the_user_model_has_no_forced_password_field():
    """The other half: nothing reads or writes it, because it no longer exists.

    Checked on `User`, which is where the column lived -- not on `SignupRequest`,
    which never had it.
    """
    field_names = {f.name for f in User._meta.get_fields()}
    assert "must_change_password" not in field_names


def test_password_change_still_works_and_touches_no_flag(trainer):
    """The ordinary self-service path survives; there is simply no flag to clear."""
    client = Client()
    client.force_login(trainer)

    assert client.get(reverse("accounts:password-change")).status_code == 200

    response = client.post(
        reverse("accounts:password-change"),
        {
            "old_password": GOOD_PASSWORD,
            "new_password1": "An0ther-Good-Passphrase-7",
            "new_password2": "An0ther-Good-Passphrase-7",
        },
    )
    if response.status_code == 200:
        pytest.fail(f"password change form rejected: {response.context['form'].errors}")
    assert response.status_code == 302

    trainer.refresh_from_db()
    assert trainer.check_password("An0ther-Good-Passphrase-7")


@pytest.mark.parametrize("view_name", ["accounts:logout", "admin:logout"])
def test_logout_url_names_resolve(view_name):
    """Kept from the original section 3.

    The defect it caught was an allow-list naming routes that did not resolve, so
    the exemption silently never applied. There is no allow-list now, but both
    names are still referenced by the auth tests and a typo in either would
    redirect logout into a 404, which is the same class of confusing failure.
    """
    from django.urls import resolve

    match = resolve(reverse(view_name))
    assert match.view_name == view_name


# ---------------------------------------------------------------------------
# 4. Only admins decide; nobody self-approves.
# ---------------------------------------------------------------------------


def test_a_trainer_cannot_approve(admin, trainer):
    result = _apply(email="guarded@example.test", name="Guarded")
    with pytest.raises(DecisionError):
        decide(approver=trainer, request_pk=result.request.pk, approve=True)

    result.request.refresh_from_db()
    assert result.request.status == "pending"


def test_an_admin_cannot_approve_their_own_application(admin):
    result = _apply(email="self@example.test", name="Self")
    # Force the pathological case: the approver is the applicant.
    result.request.user = admin
    result.request.save(update_fields=["user"])

    with pytest.raises(DecisionError):
        decide(approver=admin, request_pk=result.request.pk, approve=True)


def test_decision_is_final(admin):
    result = _apply(email="final@example.test", name="Final")
    decide(approver=admin, request_pk=result.request.pk, approve=True)

    with pytest.raises(DecisionError):
        decide(approver=admin, request_pk=result.request.pk, approve=False)

    result.request.refresh_from_db()
    assert result.request.status == "approved"


def test_rejection_marks_user_rejected(admin):
    result = _apply(email="denied@example.test", name="Denied")
    decide(approver=admin, request_pk=result.request.pk, approve=False, note="not now")

    result.user.refresh_from_db()
    assert result.user.approval_status == "rejected"


def test_rejected_email_cannot_re_register(admin):
    _apply(email="blocked@example.test", name="Blocked")
    request = SignupRequest.objects.get(email="blocked@example.test")
    decide(approver=admin, request_pk=request.pk, approve=False)

    from apps.accounts.service.signup import SignupError

    with pytest.raises(SignupError):
        _apply(email="blocked@example.test", name="Blocked")


def test_a_pending_user_cannot_approve(admin, trainee):
    result = _apply(email="pending.approver@example.test", name="Pending Approver")
    with pytest.raises(DecisionError):
        decide(approver=trainee, request_pk=result.request.pk, approve=True)

    result.request.refresh_from_db()
    assert result.request.status == "pending"


# ---------------------------------------------------------------------------
# 5. Signup creates an inert pending account, with no role.
# ---------------------------------------------------------------------------


def test_signup_creates_pending_and_cannot_log_in():
    result = _apply(email="inert@example.test", name="Inert")
    assert result.user.approval_status == "pending"
    assert result.user.is_approved is False
    assert result.request.status == "pending"
    assert result.user.check_password(GOOD_PASSWORD) is True
    # D45: pending means *inactive*, so `ModelBackend` refuses the credentials
    # before any view-specific check runs. Every backend, not just ours.
    assert result.user.is_active is False


def test_signup_rejects_a_trainee_registering_somebody_else(trainee):
    """A trainee must not become an identity factory (D41).

    This is the guard behind `can_register`. Before the rewrite, any signed-in
    approved user could POST the signup form and create other accounts.
    """
    from apps.accounts.service.signup import SignupError

    with pytest.raises(SignupError):
        submit_application(
            email="x@example.test",
            full_name="X",
            password=GOOD_PASSWORD,
            requested_role="trainee",
            requester=trainee,
        )


def test_signup_rejects_a_duplicate_email():
    from apps.accounts.service.signup import SignupError

    _apply(email="dupe@example.test", name="Dupe")
    with pytest.raises(SignupError):
        _apply(email="dupe@example.test", name="Dupe")


def test_a_trainer_may_register_on_behalf_and_the_row_records_who(trainer):
    """The on-behalf path, and the reason it exists as its own test: `created_by`
    is the only durable record of who filled the form in. Before it, that lived in
    a log line and died when the logs rotated."""
    result = submit_application(
        email="onbehalf@example.test",
        full_name="On Behalf",
        password=GOOD_PASSWORD,
        requested_role="trainer",
        requester=trainer,
    )

    assert result.request.created_by_id == trainer.pk
    # And still inert, exactly as a self-registration would be.
    assert result.user.approval_status == "pending"
    assert result.user.is_active is False
    assert roles_of(result.user) == []
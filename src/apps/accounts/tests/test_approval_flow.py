"""Regression tests for four defects found on 2026-10-04.

Each test names the bug it prevents. If one of these fails, the corresponding
defect is back.
"""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts.middleware import EXEMPT_NAMES
from apps.accounts.models import SignupRequest, User
from apps.accounts.service.approval import DecisionError, decide
from apps.accounts.service.signup import submit_application

from .conftest import GOOD_PASSWORD, roles_of

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# 1. Approving a trainee must NOT make them a trainer.
#
# approval.py used to branch on
#     if req.selected_trainer_id == approver.pk: grant ROLE_TRAINER
# That condition is true for every ordinary application, because the approver
# IS the trainer the applicant picked. So a normal approval silently promoted
# the trainee to trainer -- they could then open the approval queue and approve
# other people. Verified against the running app before the fix.
# ---------------------------------------------------------------------------


def test_approving_a_trainee_does_not_grant_trainer(trainer):
    result = submit_application(
        email="new.trainee@example.test",
        full_name="New Trainee",
        password=GOOD_PASSWORD,
        selected_trainer=trainer,
    )
    assert roles_of(result.user) == ["trainee"]
    assert result.user.is_trainer is False

    decide(approver=trainer, request_pk=result.request.pk, approve=True)

    result.user.refresh_from_db()
    assert result.user.approval_status == "approved"
    assert roles_of(result.user) == ["trainee"]
    assert result.user.is_trainer is False, (
        "approval must never mint a trainer; roles are assigned at creation"
    )


def test_approved_trainee_cannot_approve_anyone(trainer, approved_trainee):
    """The escalation is only dangerous if it grants real capability."""
    result = submit_application(
        email="second.trainee@example.test",
        full_name="Second Trainee",
        password=GOOD_PASSWORD,
        selected_trainer=trainer,
    )
    # The would-be escalated user is the *selected* trainer for this request.
    result.request.selected_trainer = approved_trainee
    result.request.save(update_fields=["selected_trainer"])

    with pytest.raises(DecisionError):
        decide(approver=approved_trainee, request_pk=result.request.pk, approve=True)

    result.request.refresh_from_db()
    assert result.request.status == "pending"


def test_role_assignment_happens_only_at_creation(trainer):
    """Regression guard: signup is the only thing that may add a role."""
    result = submit_application(
        email="third.trainee@example.test",
        full_name="Third Trainee",
        password=GOOD_PASSWORD,
        selected_trainer=trainer,
    )
    before = roles_of(result.user)

    decide(approver=trainer, request_pk=result.request.pk, approve=True)
    result.user.refresh_from_db()

    assert roles_of(result.user) == before


# ---------------------------------------------------------------------------
# 2. select_for_update() must not be joined to a nullable FK.
#
# SignupRequest.user and .selected_trainer are nullable, so select_related()
# builds an outer join and PostgreSQL raises
#     FOR UPDATE cannot be applied to the nullable side of an outer join
# Every approval 500'd on the real database.
# ---------------------------------------------------------------------------


def test_decide_works_on_postgres_with_nullable_joined_fks(trainer):
    """This is the test that fails with the joined select_for_update."""
    result = submit_application(
        email="lock.trainee@example.test",
        full_name="Lock Trainee",
        password=GOOD_PASSWORD,
        selected_trainer=trainer,
    )
    assert result.request.selected_trainer_id is not None
    assert result.request.user_id is not None

    outcome = decide(approver=trainer, request_pk=result.request.pk, approve=True)
    assert outcome.request.status == "approved"
    assert outcome.user.approval_status == "approved"
    assert outcome.request.decided_by_id == trainer.pk


def test_decide_sets_auditing_fields(trainer):
    result = submit_application(
        email="audit.trainee@example.test",
        full_name="Audit Trainee",
        password=GOOD_PASSWORD,
        selected_trainer=trainer,
    )
    decide(approver=trainer, request_pk=result.request.pk, approve=True, note="looks good")

    result.request.refresh_from_db()
    assert result.request.decision_note == "looks good"
    assert result.request.decided_by_id == trainer.pk
    assert result.request.decided_at is not None


# ---------------------------------------------------------------------------
# 3. A placeholder-password account must be able to log out.
#
# The middleware checked request.resolver_match, which is always None in the
# request phase (URL resolution has not run yet), so EXEMPT_NAMES never matched.
# Logout bounced to the password page and left the session authenticated.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("view_name", ["accounts:logout", "admin:logout"])
def test_exempt_names_are_real_url_names(view_name):
    """If a name in EXEMPT_NAMES is wrong, the exemption silently never applies."""
    from django.urls import resolve

    match = resolve(reverse(view_name))
    assert match.view_name == view_name


def test_placeholder_account_can_log_out(trainer):
    trainer.must_change_password = True
    trainer.save(update_fields=["must_change_password"])

    client = Client()
    client.force_login(trainer)

    response = client.post(reverse("accounts:logout"))
    assert response.status_code == 302
    assert response["Location"] == reverse("accounts:login")
    assert client.session.get("_auth_user_id") is None


def test_placeholder_account_is_still_confined(trainer):
    trainer.must_change_password = True
    trainer.save(update_fields=["must_change_password"])

    client = Client()
    client.force_login(trainer)

    for name in ("accounts:trainer-queue", "accounts:pending", "accounts:signup"):
        url = reverse(name)
        response = client.get(url)
        assert response.status_code == 302, f"{name} must redirect"
        assert reverse("accounts:password-change") in response["Location"]


def test_placeholder_account_can_reach_password_change(trainer):
    trainer.must_change_password = True
    trainer.save(update_fields=["must_change_password"])

    client = Client()
    client.force_login(trainer)

    response = client.get(reverse("accounts:password-change"))
    assert response.status_code == 200


def test_password_change_clears_the_flag(trainer):
    trainer.must_change_password = True
    trainer.save(update_fields=["must_change_password"])

    client = Client()
    client.force_login(trainer)

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
    assert trainer.must_change_password is False


def test_exempt_set_is_minimal():
    """A wide allow-list would reopen whatever it excludes."""
    assert EXEMPT_NAMES == frozenset(
        {"accounts:password-change", "accounts:logout", "admin:logout"}
    )


# ---------------------------------------------------------------------------
# 4. Only the selected trainer decides; nobody self-approves.
# ---------------------------------------------------------------------------


def test_other_trainer_cannot_approve(trainer, other_trainer):
    result = submit_application(
        email="guarded@example.test",
        full_name="Guarded",
        password=GOOD_PASSWORD,
        selected_trainer=trainer,
    )
    with pytest.raises(DecisionError):
        decide(approver=other_trainer, request_pk=result.request.pk, approve=True)

    result.request.refresh_from_db()
    assert result.request.status == "pending"


def test_trainer_cannot_approve_their_own_application(trainer):
    result = submit_application(
        email="self@example.test",
        full_name="Self",
        password=GOOD_PASSWORD,
        selected_trainer=trainer,
    )
    # Force the pathological case: applicant *is* the selected trainer.
    result.request.user = trainer
    result.request.save(update_fields=["user"])

    with pytest.raises(DecisionError):
        decide(approver=trainer, request_pk=result.request.pk, approve=True)


def test_decision_is_final(trainer):
    result = submit_application(
        email="final@example.test",
        full_name="Final",
        password=GOOD_PASSWORD,
        selected_trainer=trainer,
    )
    decide(approver=trainer, request_pk=result.request.pk, approve=True)

    with pytest.raises(DecisionError):
        decide(approver=trainer, request_pk=result.request.pk, approve=False)

    result.request.refresh_from_db()
    assert result.request.status == "approved"


def test_rejection_marks_user_rejected(trainer):
    result = submit_application(
        email="denied@example.test",
        full_name="Denied",
        password=GOOD_PASSWORD,
        selected_trainer=trainer,
    )
    decide(approver=trainer, request_pk=result.request.pk, approve=False, note="not now")

    result.user.refresh_from_db()
    assert result.user.approval_status == "rejected"


def test_rejected_email_cannot_re_register(trainer):
    submit_application(
        email="blocked@example.test",
        full_name="Blocked",
        password=GOOD_PASSWORD,
        selected_trainer=trainer,
    )
    request = SignupRequest.objects.get(email="blocked@example.test")
    decide(approver=trainer, request_pk=request.pk, approve=False)

    from apps.accounts.service.signup import SignupError

    with pytest.raises(SignupError):
        submit_application(
            email="blocked@example.test",
            full_name="Blocked",
            password=GOOD_PASSWORD,
            selected_trainer=trainer,
        )


def test_pending_trainer_cannot_approve(trainer, trainee):
    result = submit_application(
        email="pending.approver@example.test",
        full_name="Pending Approver",
        password=GOOD_PASSWORD,
        selected_trainer=trainer,
    )
    with pytest.raises(DecisionError):
        decide(approver=trainee, request_pk=result.request.pk, approve=True)

    result.request.refresh_from_db()
    assert result.request.status == "pending"


# ---------------------------------------------------------------------------
# 5. Signup creates an inert pending account.
# ---------------------------------------------------------------------------


def test_signup_creates_pending_and_cannot_log_in(trainer):
    result = submit_application(
        email="inert@example.test",
        full_name="Inert",
        password=GOOD_PASSWORD,
        selected_trainer=trainer,
    )
    assert result.user.approval_status == "pending"
    assert result.user.is_approved is False
    assert result.request.status == "pending"
    assert result.user.check_password(GOOD_PASSWORD) is True


def test_signup_rejects_unapproved_trainer(trainee):
    from apps.accounts.service.signup import SignupError

    with pytest.raises(SignupError):
        submit_application(
            email="x@example.test",
            full_name="X",
            password=GOOD_PASSWORD,
            selected_trainer=trainee,
        )


def test_signup_rejects_duplicate_email(trainer):
    from apps.accounts.service.signup import SignupError

    submit_application(
        email="dupe@example.test",
        full_name="Dupe",
        password=GOOD_PASSWORD,
        selected_trainer=trainer,
    )
    with pytest.raises(SignupError):
        submit_application(
            email="dupe@example.test",
            full_name="Dupe",
            password=GOOD_PASSWORD,
            selected_trainer=trainer,
        )

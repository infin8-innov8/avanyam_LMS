"""Signup service: the flow that creates accounts.

`submit_application` is the only place a self-registered account comes into
existence, so the invariants worth pinning are the ones §6.2 and §15 promise:

* a signup is **inert** -- pending, trainee, and with `must_change_password` set
* a **rejected** email is a durable blocklist entry and cannot silently re-register
* the writes are **one transaction**: no User without its SignupRequest
* the trainer is **notified only after commit**
* signup is behind a **kill switch** that needs no redeploy
"""

from __future__ import annotations

import pytest
from django.core import mail
from django.db import transaction

from apps.accounts.models import ROLE_ADMIN, ROLE_TRAINEE, ROLE_TRAINER, SignupRequest, User
from apps.accounts.service.signup import SignupError, signup_enabled, submit_application

from .conftest import GOOD_PASSWORD, make_user

pytestmark = pytest.mark.django_db


def signup(trainer, email="fresh@example.test", **overrides):
    kwargs = {
        "email": email,
        "full_name": "Fresh Applicant",
        "password": GOOD_PASSWORD,
        "selected_trainer": trainer,
    }
    kwargs.update(overrides)
    return submit_application(**kwargs)


# ---------------------------------------------------------------------------
# The inertness invariant
# ---------------------------------------------------------------------------


def test_signup_creates_a_pending_trainee(trainer) -> None:
    result = signup(trainer)

    user = result.user
    assert user.approval_status == "pending"
    assert user.is_approved is False
    assert user.is_trainer is False
    assert user.auth_source == "signup"
    assert user.email == "fresh@example.test"
    assert sorted(user.role_assignments.values_list("role__slug", flat=True)) == [ROLE_TRAINEE]


def test_signup_links_the_request_to_the_user(trainer) -> None:
    """A SignupRequest with no User is what breaks the rejected-email blocklist."""
    result = signup(trainer)

    request = SignupRequest.objects.get(pk=result.request.pk)
    assert request.user_id == result.user.pk
    assert request.status == "pending"
    assert request.selected_trainer_id == trainer.pk


def test_signup_never_stores_a_plaintext_password(trainer) -> None:
    result = signup(trainer)
    assert result.user.password != GOOD_PASSWORD
    assert result.user.check_password(GOOD_PASSWORD)
    assert not hasattr(result.request, "password")


def test_signup_does_not_force_a_password_change(trainer) -> None:
    """`must_change_password` is for *predictable* passwords, not self-chosen ones.

    The seeded staff accounts arrive with a password derived from the person's
    name, so they must replace it. A signup user typed their own password, so
    demanding they immediately replace it teaches them to ignore the prompt.
    """
    assert signup(trainer).user.must_change_password is False


def test_full_name_is_whitespace_normalised(trainer) -> None:
    result = signup(trainer, full_name="  Spaced   Out  Name ")
    assert result.user.full_name == "Spaced Out Name"
    assert result.request.full_name == "Spaced Out Name"


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_signup_refused_when_disabled(trainer, settings) -> None:
    """The kill switch (§15): flip it, no redeploy, no account."""
    settings.ACCOUNTS = {**settings.ACCOUNTS, "SIGNUP_ENABLED": False}

    with pytest.raises(SignupError, match="currently closed"):
        signup(trainer)

    assert not User.objects.filter(email="fresh@example.test").exists()


def test_signup_enabled_reflects_settings(settings) -> None:
    settings.ACCOUNTS = {**settings.ACCOUNTS, "SIGNUP_ENABLED": True}
    assert signup_enabled() is True
    settings.ACCOUNTS = {**settings.ACCOUNTS, "SIGNUP_ENABLED": False}
    assert signup_enabled() is False


def test_duplicate_email_is_refused_case_insensitively(trainer) -> None:
    signup(trainer, email="dupe@example.test")

    with pytest.raises(SignupError, match="already exists"):
        signup(trainer, email="DUPE@EXAMPLE.TEST")

    assert User.objects.filter(email__iexact="dupe@example.test").count() == 1


def test_rejected_email_cannot_re_register(trainer) -> None:
    """§6.2: the SignupRequest row is the durable blocklist.

    Deleting the User alone must not let the address back in -- otherwise
    "rejected" is only a UI state and not a real decision.
    """
    result = signup(trainer, email="rejected@example.test")
    result.request.transition("rejected", by=trainer)
    result.request.save()
    result.user.delete()

    assert not User.objects.filter(email__iexact="rejected@example.test").exists()

    with pytest.raises(SignupError, match="declined previously"):
        signup(trainer, email="rejected@example.test")

    assert not User.objects.filter(email__iexact="rejected@example.test").exists()


def test_pending_application_blocks_a_second_signup(trainer) -> None:
    signup(trainer, email="pending.twice@example.test")
    User.objects.filter(email__iexact="pending.twice@example.test").delete()

    with pytest.raises(SignupError, match="already exists"):
        signup(trainer, email="pending.twice@example.test")


# ---------------------------------------------------------------------------
# Trainer eligibility
# ---------------------------------------------------------------------------


def test_pending_trainer_is_refused(trainer) -> None:
    unapproved = make_user("unapproved.trainer@example.test", role=ROLE_TRAINER, approved=False)
    with pytest.raises(SignupError, match="not available"):
        signup(unapproved)


def test_inactive_trainer_is_refused(trainer) -> None:
    trainer.is_active = False
    trainer.save(update_fields=["is_active"])
    with pytest.raises(SignupError, match="not available"):
        signup(trainer)


def test_trainee_is_refused_as_a_trainer(approved_trainee) -> None:
    """Otherwise a trainee could be nominated to approve their own peer."""
    with pytest.raises(SignupError, match="not available"):
        signup(approved_trainee)


def test_admin_is_accepted_as_trainer(admin) -> None:
    """Admins can decide applications, so they are a valid choice."""
    result = signup(admin, email="to.admin@example.test")
    assert result.request.selected_trainer_id == admin.pk


# ---------------------------------------------------------------------------
# Submitting on someone else's behalf
# ---------------------------------------------------------------------------


def test_approved_staff_may_submit_for_someone_else(trainer) -> None:
    result = signup(trainer, email="on.behalf@example.test", requester=trainer)
    assert result.user.email == "on.behalf@example.test"


def test_pending_requester_may_not_submit_for_someone_else(trainer, trainee) -> None:
    with pytest.raises(SignupError, match="approved staff"):
        signup(trainer, email="sneaky@example.test", requester=trainee)

    assert not User.objects.filter(email="sneaky@example.test").exists()


# ---------------------------------------------------------------------------
# Transactional integrity
# ---------------------------------------------------------------------------


def test_nothing_is_written_when_the_request_creation_fails(trainer, monkeypatch) -> None:
    """A half-applied signup is the failure mode this service exists to prevent.

    Forcing the SignupRequest insert to fail must leave *no* User behind --
    otherwise the next signup for that address hits "account already exists" with
    no application to approve.
    """
    original_create = SignupRequest.objects.create

    def boom(*args, **kwargs):
        raise RuntimeError("simulated failure creating the request")

    monkeypatch.setattr(SignupRequest.objects, "create", boom)

    with pytest.raises(RuntimeError):
        signup(trainer, email="half.written@example.test")

    monkeypatch.setattr(SignupRequest.objects, "create", original_create)
    assert not User.objects.filter(email="half.written@example.test").exists()
    assert not SignupRequest.objects.filter(email="half.written@example.test").exists()


def test_trainer_is_notified_after_commit_only(trainer, django_capture_on_commit_callbacks) -> None:
    """The notification is deferred to commit, not sent inside the transaction.

    `on_commit` does not fire while pytest holds the test open in its own
    transaction, so the callback is captured and executed explicitly here. That is
    also what makes this test meaningful: it shows the send is *deferred*, because
    `mail.outbox` is still empty on the line after `signup()` returns.
    """
    with django_capture_on_commit_callbacks(execute=True):
        signup(trainer, email="notified@example.test")
        assert mail.outbox == [], "notification was sent before the commit"

    assert len(mail.outbox) == 1, "trainer was not told about a real application"
    assert mail.outbox[0].to == [trainer.email]


def test_no_notification_when_the_transaction_rolls_back(trainer, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(SignupRequest.objects, "create", boom)

    with pytest.raises(RuntimeError):
        signup(trainer, email="phantom@example.test")

    assert mail.outbox == [], "a trainer was emailed about an application that never existed"
    assert not SignupRequest.objects.filter(email="phantom.example.test").exists()


def test_notification_targets_the_chosen_trainer(
    trainer, other_trainer, django_capture_on_commit_callbacks
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        signup(other_trainer, email="wrong.trainer@example.test")
    assert mail.outbox[0].to == [other_trainer.email]
    assert trainer.email not in mail.outbox[0].to

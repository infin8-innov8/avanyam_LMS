"""Signup service: the flow that creates accounts.

`submit_application` is the only place a self-registered account comes into
existence, so the invariants worth pinning are the ones §6.2 and §15 promise:

* a signup is **inert** -- pending, inactive, and holding *no* role (D49). It used
  to be created as a trainee; a pending trainee already appeared in every query
  filtering on role.
* a **rejected** email is a durable blocklist entry and cannot silently re-register
* the writes are **one transaction**: no User without its SignupRequest
* **admins** are notified only after commit -- every admin, since there is one
  shared queue and nobody is named on an application any more (D41)
* signup is behind a **kill switch** that needs no redeploy
"""

from __future__ import annotations

import pytest
from django.core import mail

from apps.accounts.models import (
    ROLE_ADMIN,
    SignupRequest,
    User,
)
from apps.accounts.service.signup import SignupError, signup_enabled, submit_application

from .conftest import GOOD_PASSWORD, make_user

pytestmark = pytest.mark.django_db


def signup(email="fresh@example.test", *, requested_role="trainee", **overrides):
    """Submit an application. Defaults to asking to be a trainee.

    The first positional argument used to be the trainer the applicant nominated.
    It is now the requested role, which is what an applicant actually chooses.
    """
    kwargs = {
        "email": email,
        "full_name": "Fresh Applicant",
        "password": GOOD_PASSWORD,
        "requested_role": requested_role,
    }
    kwargs.update(overrides)
    return submit_application(**kwargs)


# ---------------------------------------------------------------------------
# The inertness invariant
# ---------------------------------------------------------------------------


def test_signup_creates_an_inert_account() -> None:
    """Pending, inactive, and holding no role at all.

    The role assertion is the one that matters. Creating a pending *trainee* made
    an unapproved person already appear in every "who is a trainee" query, and a
    pending *trainer* would have looked like an approver to anything filtering on
    role rather than approval status.
    """
    result = signup()

    user = result.user
    assert user.approval_status == "pending"
    assert user.is_approved is False
    assert user.is_trainer is False
    assert user.auth_source == "signup"
    assert user.email == "fresh@example.test"
    # Inactive, so no backend can authenticate them (D45/D50).
    assert user.is_active is False
    assert list(user.role_assignments.all()) == []
    assert result.request.requested_role == "trainee"


def test_a_trainer_application_also_carries_no_role() -> None:
    """Asking to be a trainer must not pre-grant the trainer role either."""
    result = signup("wants.trainer@example.test", requested_role="trainer")

    assert result.request.requested_role == "trainer"
    assert list(result.user.role_assignments.all()) == []


def test_an_admin_cannot_be_requested() -> None:
    """Admin is minted by `manage.py createadmin`, never requested.

    Left as a refusal test because a form that omitted the choice would otherwise
    quietly work for anyone who hand-edits the POST.
    """
    with pytest.raises(SignupError):
        signup("wants.admin@example.test", requested_role="admin")


def test_signup_links_the_request_to_the_user() -> None:
    """A SignupRequest with no User is what breaks the rejected-email blocklist."""
    result = signup()

    request = SignupRequest.objects.get(pk=result.request.pk)
    assert request.user_id == result.user.pk
    assert request.status == "pending"
    assert request.selected_trainer_id is None, "nobody is named on an application"


def test_signup_never_stores_a_plaintext_password() -> None:
    result = signup()
    assert result.user.password != GOOD_PASSWORD
    assert result.user.check_password(GOOD_PASSWORD)
    assert not hasattr(result.request, "password")


def test_signup_does_not_force_a_password_change() -> None:
    """The field is gone; the password a signup user typed is the password they have.

    `must_change_password` existed to cover *predictable* seeded passwords. It
    also produced a second sign-in outcome and a redirect that could discard a
    submission. Both are gone (D43).
    """
    assert not hasattr(signup().user, "must_change_password")


def test_full_name_is_whitespace_normalised(trainer) -> None:
    result = signup(full_name="  Spaced   Out  Name ")
    assert result.user.full_name == "Spaced Out Name"
    assert result.request.full_name == "Spaced Out Name"


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_signup_refused_when_disabled(trainer, settings) -> None:
    """The kill switch (§15): flip it, no redeploy, no account."""
    settings.ACCOUNTS = {**settings.ACCOUNTS, "SIGNUP_ENABLED": False}

    with pytest.raises(SignupError, match="currently closed"):
        signup()

    assert not User.objects.filter(email="fresh@example.test").exists()


def test_signup_enabled_reflects_settings(settings) -> None:
    settings.ACCOUNTS = {**settings.ACCOUNTS, "SIGNUP_ENABLED": True}
    assert signup_enabled() is True
    settings.ACCOUNTS = {**settings.ACCOUNTS, "SIGNUP_ENABLED": False}
    assert signup_enabled() is False


def test_duplicate_email_is_refused_case_insensitively(trainer) -> None:
    signup(email="dupe@example.test")

    with pytest.raises(SignupError, match="already exists"):
        signup(email="DUPE@EXAMPLE.TEST")

    assert User.objects.filter(email__iexact="dupe@example.test").count() == 1


def test_rejected_email_cannot_re_register(trainer) -> None:
    """§6.2: the SignupRequest row is the durable blocklist.

    Deleting the User alone must not let the address back in -- otherwise
    "rejected" is only a UI state and not a real decision.
    """
    result = signup(email="rejected@example.test", requester=trainer)
    result.request.transition("rejected", by=trainer)
    result.request.save()
    result.user.delete()

    assert not User.objects.filter(email__iexact="rejected@example.test").exists()

    with pytest.raises(SignupError, match="declined previously"):
        signup(email="rejected@example.test", requester=trainer)

    assert not User.objects.filter(email__iexact="rejected@example.test").exists()


def test_pending_application_blocks_a_second_signup(trainer) -> None:
    signup(email="pending.twice@example.test")
    User.objects.filter(email__iexact="pending.twice@example.test").delete()

    with pytest.raises(SignupError, match="already exists"):
        signup(email="pending.twice@example.test")


# ---------------------------------------------------------------------------
# Submitting on someone else's behalf
#
# These replace a block that tested which users were eligible to be *named* as
# the approving trainer. Nobody is named any more, so "may this person appear on
# an application" collapsed into one question: may this person register anyone.
# ---------------------------------------------------------------------------


def test_approved_trainer_may_submit_for_someone_else(trainer) -> None:
    result = signup(email="on.behalf@example.test", requester=trainer)
    assert result.user.email == "on.behalf@example.test"
    assert result.request.created_by_id == trainer.pk


def test_approved_admin_may_submit_for_someone_else(admin) -> None:
    result = signup(email="admin.made.this@example.test", requester=admin)
    assert result.request.created_by_id == admin.pk


def test_approved_trainee_may_not_submit_for_someone_else(approved_trainee) -> None:
    """Registering for others is a Trainer/Admin capability, not a Trainee one.

    Asserted on the message because the two refusals are deliberately different:
    an unapproved requester is "not approved", a trainee is "only trainers and
    admins". Collapsing them would hide which rule actually stopped the request.
    """
    with pytest.raises(SignupError, match="Only trainers and admins"):
        signup(email="sneaky@example.test", requester=approved_trainee)

    assert not User.objects.filter(email="sneaky@example.test").exists()


def test_pending_requester_may_not_submit_for_someone_else(trainee) -> None:
    with pytest.raises(SignupError, match="not approved"):
        signup(email="sneaky2@example.test", requester=trainee)

    assert not User.objects.filter(email="sneaky2@example.test").exists()


def test_inactive_trainer_may_not_submit_for_someone_else(trainer) -> None:
    """A disabled Trainer is not an approved user, so the register gate shuts."""
    trainer.is_active = False
    trainer.save(update_fields=["is_active"])

    with pytest.raises(SignupError, match="not approved"):
        signup(email="sneaky3@example.test", requester=trainer)


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
        signup(email="half.written@example.test")

    monkeypatch.setattr(SignupRequest.objects, "create", original_create)
    assert not User.objects.filter(email="half.written@example.test").exists()
    assert not SignupRequest.objects.filter(email="half.written@example.test").exists()


def test_admins_are_notified_after_commit_only(admin, django_capture_on_commit_callbacks) -> None:
    """The notification is deferred to commit, not sent inside the transaction.

    `on_commit` does not fire while pytest holds the test open in its own
    transaction, so the callback is captured and executed explicitly here. That is
    also what makes this test meaningful: it shows the send is *deferred*, because
    `mail.outbox` is still empty on the line after `signup()` returns.
    """
    with django_capture_on_commit_callbacks(execute=True):
        signup(email="notified@example.test")
        assert mail.outbox == [], "notification was sent before the commit"

    assert len(mail.outbox) == 1, "admins were not told about a real application"
    assert admin.email in mail.outbox[0].to


def test_no_notification_when_the_transaction_rolls_back(trainer, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(SignupRequest.objects, "create", boom)

    with pytest.raises(RuntimeError):
        signup(email="phantom@example.test")

    assert mail.outbox == [], "an admin was emailed about an application that never existed"
    assert not SignupRequest.objects.filter(email="phantom.example.test").exists()


def test_notification_reaches_every_admin_not_one_nominated_trainer(
    admin, trainer, django_capture_on_commit_callbacks
) -> None:
    """Replaces a test asserting the opposite: only the chosen trainer was told.

    There is no chosen trainer any more. Under a shared queue, "who is notified"
    is "whoever can decide", so a test in the old shape would pin behaviour the
    design deliberately removed.

    Membership rather than equality, because the suite runs against a long-lived
    development database that already holds real admins.
    """
    second = make_user("second.admin@example.test", role=ROLE_ADMIN)

    with django_capture_on_commit_callbacks(execute=True):
        signup(email="told.to.everybody@example.test")

    recipients = set(mail.outbox[0].to)
    assert {admin.email, second.email} <= recipients
    assert trainer.email not in recipients, "a trainer cannot decide applications"

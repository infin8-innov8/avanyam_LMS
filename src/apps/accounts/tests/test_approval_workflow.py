"""The three-way approval decision, and reversing a permanent decline.

Split from `test_views_authz` on purpose: those tests ask "may this person see
the page", these ask "what actually happened to the row". A policy test that
also asserted the resulting status would pass while the service quietly wrote
the wrong value.
"""

from __future__ import annotations

import pytest
from django.core import mail
from django.urls import reverse

from apps.accounts.domain.enums import ApprovalStatus
from apps.accounts.models import (
    APPROVAL_UNDO_MAX_ATTEMPTS,
    ROLE_ADMIN,
    ApprovalUndoToken,
    SignupRequest,
)
from apps.accounts.service.approval import Decision, DecisionError, decide
from apps.accounts.service.signup import submit_application
from apps.accounts.policies import is_approved
from apps.accounts.service.undo import UndoError, request_undo_code, undo_rejection
from apps.accounts.tests.conftest import make_user, roles_of

pytestmark = pytest.mark.django_db


QUEUE = reverse("accounts:trainer-queue")


def _apply(trainer, name="Kiran Rao", email="kiran@example.com", **form):
    """Submit an application the way the public form does, and return the row."""
    submit_application(
        full_name=name,
        email=email,
        password="Str0ng-Pass!x9",
        selected_trainer=trainer,
        **form,
    )
    return SignupRequest.objects.get(email=email)


# ---------------------------------------------------------------------------
# The three decisions
# ---------------------------------------------------------------------------


def test_there_are_exactly_three_decisions() -> None:
    assert [d.value for d in Decision] == ["approve", "reject", "redirect"]


def test_approve_moves_the_row_and_the_user_together(trainer) -> None:
    req = _apply(trainer)

    decide(approver=trainer, request_pk=req.pk, decision="approve")

    req.refresh_from_db()
    assert req.status == ApprovalStatus.APPROVED
    assert req.user.approval_status == ApprovalStatus.APPROVED


def test_reject_is_permanent_and_keeps_the_user_inert(trainer) -> None:
    req = _apply(trainer)

    decide(approver=trainer, request_pk=req.pk, decision="reject", note="No.")

    req.refresh_from_db()
    assert req.status == ApprovalStatus.REJECTED
    # Still linked, so the undo can find it again -- but not usable.
    assert req.user.approval_status == ApprovalStatus.REJECTED
    # `is_approved` is the master gate the login view and every policy consult.
    assert not is_approved(req.user)


def test_redirect_closes_the_account_but_leaves_the_address_open(trainer) -> None:
    """The distinction that matters: a soft decline must not block re-registering."""
    req = _apply(trainer)

    decide(approver=trainer, request_pk=req.pk, decision="redirect", note="Wrong trainer.")

    req.refresh_from_db()
    assert req.status == ApprovalStatus.REDIRECTED
    assert req.user_id is None, "a soft decline should not leave a dead account behind"


def test_a_redirected_address_may_register_again(trainer, other_trainer) -> None:
    other = other_trainer
    _apply(trainer)
    first = SignupRequest.objects.get(email="kiran@example.com")
    decide(approver=trainer, request_pk=first.pk, decision="redirect")

    second = submit_application(
        full_name="Kiran Rao",
        email="kiran@example.com",
        password="An0ther-Pass!x9",
        selected_trainer=other,
    )

    assert second.request.status == ApprovalStatus.PENDING
    assert SignupRequest.objects.filter(email="kiran@example.com").count() == 2


def test_a_rejected_address_may_not_register_again(trainer, other_trainer) -> None:
    req = _apply(trainer)
    decide(approver=trainer, request_pk=req.pk, decision="reject")

    from apps.accounts.service.signup import SignupError

    with pytest.raises(SignupError):
        submit_application(
            full_name="Kiran Rao",
            email="kiran@example.com",
            password="An0ther-Pass!x9",
            selected_trainer=other_trainer,
        )


def test_an_approved_address_may_not_register_again(trainer, other_trainer) -> None:
    req = _apply(trainer)
    decide(approver=trainer, request_pk=req.pk, decision="approve")

    from apps.accounts.service.signup import SignupError

    with pytest.raises(SignupError):
        submit_application(
            full_name="Kiran Rao",
            email="kiran@example.com",
            password="An0ther-Pass!x9",
            selected_trainer=other_trainer,
        )


def test_an_unknown_decision_is_refused_rather_than_defaulted(trainer) -> None:
    """A typo must not be read as 'approve' or quietly land on some other branch."""
    req = _apply(trainer)

    with pytest.raises(DecisionError):
        decide(approver=trainer, request_pk=req.pk, decision="aprove")

    req.refresh_from_db()
    assert req.status == ApprovalStatus.PENDING


# ---------------------------------------------------------------------------
# Emails per decision
# ---------------------------------------------------------------------------


def _decision_mail(email: str) -> str:
    return next(m.body for m in mail.outbox if email in m.to)


def test_a_redirect_email_offers_to_register_again(trainer) -> None:
    req = _apply(trainer)
    decide(approver=trainer, request_pk=req.pk, decision="redirect")

    body = _decision_mail("kiran@example.com")
    assert "register again" in body.lower()
    assert reverse("accounts:signup") in _html_for("kiran@example.com")


def test_a_reject_email_says_the_address_is_blocked(trainer) -> None:
    req = _apply(trainer)
    decide(approver=trainer, request_pk=req.pk, decision="reject")

    body = _decision_mail("kiran@example.com")
    assert "cannot register again" in body.lower()


def _html_for(address: str) -> str:
    return next(m.alternatives[0][0] for m in mail.outbox if address in m.to)


# ---------------------------------------------------------------------------
# The undo codes
# ---------------------------------------------------------------------------


def _rejected(trainer, other_trainer=None) -> SignupRequest:
    req = _apply(trainer)
    decide(approver=trainer, request_pk=req.pk, decision="reject", note="Wrong branch.")
    req.refresh_from_db()
    return req


def test_requesting_a_code_emails_the_trainer_and_not_the_applicant(trainer) -> None:
    req = _rejected(trainer)
    mail.outbox.clear()  # the decline itself already mailed the applicant

    issued = request_undo_code(trainer=trainer, request_pk=req.pk)

    assert issued.code.isdigit() and len(issued.code) == 6
    trainer_mail = [m for m in mail.outbox if trainer.email in m.to]
    assert trainer_mail, "the trainer gets the code"
    assert issued.code in trainer_mail[0].body
    # Copying the applicant would tell them a permanent refusal is being undone.
    assert not [m for m in mail.outbox if "kiran@example.com" in m.to]


def test_the_code_is_stored_hashed_not_in_the_clear(trainer) -> None:
    req = _rejected(trainer)

    issued = request_undo_code(trainer=trainer, request_pk=req.pk)

    stored = ApprovalUndoToken.objects.get(request=req)
    assert issued.code not in stored.code_hash
    assert stored.code_hash != issued.code


def test_a_valid_code_returns_the_row_to_pending(trainer) -> None:
    req = _rejected(trainer)
    issued = request_undo_code(trainer=trainer, request_pk=req.pk)

    undo_rejection(trainer=trainer, request_pk=req.pk, code=issued.code)

    req.refresh_from_db()
    assert req.status == ApprovalStatus.PENDING
    assert req.user.approval_status == ApprovalStatus.PENDING


def test_reversing_tells_the_applicant_and_names_who_did_it(trainer) -> None:
    req = _rejected(trainer)
    issued = request_undo_code(trainer=trainer, request_pk=req.pk)
    mail.outbox.clear()

    undo_rejection(trainer=trainer, request_pk=req.pk, code=issued.code)

    body = _decision_mail("kiran@example.com")
    assert "reversed" in body.lower()
    assert trainer.full_name in body


def test_a_code_cannot_be_redeemed_twice(trainer) -> None:
    req = _rejected(trainer)
    issued = request_undo_code(trainer=trainer, request_pk=req.pk)
    undo_rejection(trainer=trainer, request_pk=req.pk, code=issued.code)

    with pytest.raises(UndoError):
        undo_rejection(trainer=trainer, request_pk=req.pk, code=issued.code)


def test_a_wrong_code_is_refused_and_counted(trainer) -> None:
    req = _rejected(trainer)
    request_undo_code(trainer=trainer, request_pk=req.pk)

    with pytest.raises(UndoError):
        undo_rejection(trainer=trainer, request_pk=req.pk, code="000000")

    assert ApprovalUndoToken.objects.get(request=req).attempts == 1
    req.refresh_from_db()
    assert req.status == ApprovalStatus.REJECTED, "a wrong guess must not move the row"


def test_guessing_is_capped_so_the_code_cannot_be_brute_forced(trainer) -> None:
    req = _rejected(trainer)
    request_undo_code(trainer=trainer, request_pk=req.pk)

    for _ in range(APPROVAL_UNDO_MAX_ATTEMPTS):
        with pytest.raises(UndoError):
            undo_rejection(trainer=trainer, request_pk=req.pk, code="111111")

    # A further attempt must not even be hashed and compared.
    with pytest.raises(UndoError, match="Too many"):
        undo_rejection(trainer=trainer, request_pk=req.pk, code="222222")


def test_an_expired_code_is_refused(trainer) -> None:
    from datetime import timedelta

    from django.utils import timezone

    req = _rejected(trainer)
    issued = request_undo_code(trainer=trainer, request_pk=req.pk)
    ApprovalUndoToken.objects.filter(request=req).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )

    with pytest.raises(UndoError, match="expired"):
        undo_rejection(trainer=trainer, request_pk=req.pk, code=issued.code)


def test_asking_again_invalidates_the_first_code(trainer) -> None:
    """An abandoned code must not stay live in an old inbox."""
    req = _rejected(trainer)
    first = request_undo_code(trainer=trainer, request_pk=req.pk)
    second = request_undo_code(trainer=trainer, request_pk=req.pk)

    with pytest.raises(UndoError):
        undo_rejection(trainer=trainer, request_pk=req.pk, code=first.code)

    undo_rejection(trainer=trainer, request_pk=req.pk, code=second.code)
    req.refresh_from_db()
    assert req.status == ApprovalStatus.PENDING


# ---------------------------------------------------------------------------
# Who may undo
# ---------------------------------------------------------------------------


def test_another_trainer_cannot_undo(trainer, other_trainer) -> None:
    req = _rejected(trainer)

    with pytest.raises(UndoError):
        request_undo_code(trainer=other_trainer, request_pk=req.pk)


def test_an_admin_may_undo_any_rejection(trainer, admin) -> None:
    req = _rejected(trainer)
    issued = request_undo_code(trainer=admin, request_pk=req.pk)

    undo_rejection(trainer=admin, request_pk=req.pk, code=issued.code)

    req.refresh_from_db()
    assert req.status == ApprovalStatus.PENDING


def test_a_pending_row_offers_no_undo(trainer) -> None:
    req = _apply(trainer)

    with pytest.raises(UndoError):
        request_undo_code(trainer=trainer, request_pk=req.pk)


def test_a_trainer_cannot_undo_their_own_application(trainer) -> None:
    """Separation of duties applies to reversing a decision too.

    Otherwise a trainer could have their own application declined and immediately
    undo that decline. `can_approve` already refuses the decision itself; the undo
    must refuse it too rather than leaning on a later approval step to catch it.
    """
    # This state is not reachable through the public form: signup refuses an
    # address that already has an account, so an approved trainer cannot file an
    # application against their own address in the first place. The guard is
    # therefore defence in depth, mirroring the one in `can_approve`, and is
    # exercised here by writing the row directly rather than pretending a user
    # journey reaches it.
    req = SignupRequest.objects.create(
        full_name=trainer.full_name,
        email=trainer.email,
        user=trainer,
        selected_trainer=trainer,
        status=ApprovalStatus.PENDING,
    )
    SignupRequest.objects.filter(pk=req.pk).update(status=ApprovalStatus.REJECTED)
    assert req.user_id == trainer.pk

    with pytest.raises(UndoError, match="your own"):
        request_undo_code(trainer=trainer, request_pk=req.pk)


def test_a_trainee_cannot_undo(trainer) -> None:
    req = _rejected(trainer)
    trainee = make_user("trainee@example.com", role="trainee", approved=True)

    with pytest.raises(UndoError):
        request_undo_code(trainer=trainee, request_pk=req.pk)


# ---------------------------------------------------------------------------
# The queue page
# ---------------------------------------------------------------------------


def test_the_queue_offers_all_three_decisions(client, trainer) -> None:
    _apply(trainer)
    client.force_login(trainer)

    html = client.get(QUEUE).content.decode()

    assert 'value="approve"' in html
    assert 'value="reject"' in html
    assert 'value="redirect"' in html


def test_the_undo_form_appears_only_on_a_rejected_row(client, trainer) -> None:
    pending = _apply(trainer, email="pending@example.com")
    rejected = _rejected(trainer)
    client.force_login(trainer)

    on_pending = client.get(QUEUE).content.decode()
    on_rejected = client.get(QUEUE, {"filter": "rejected"}).content.decode()

    # Nothing to undo while the application is still awaiting a decision...
    assert reverse("accounts:undo-code", args=[pending.pk]) not in on_pending
    assert reverse("accounts:undo-confirm", args=[pending.pk]) not in on_pending
    # ...and the undo offered once it has been declined.
    assert reverse("accounts:undo-code", args=[rejected.pk]) in on_rejected
    assert reverse("accounts:undo-confirm", args=[rejected.pk]) in on_rejected


def test_another_trainer_never_sees_the_row_to_undo(client, trainer, other_trainer) -> None:
    req = _rejected(trainer)
    client.force_login(other_trainer)

    html = client.get(QUEUE, {"filter": "rejected"}).content.decode()

    assert reverse("accounts:undo-confirm", args=[req.pk]) not in html
    assert "kiran@example.com" not in html


def test_a_redirected_row_gets_its_own_tab(client, trainer) -> None:
    req = _apply(trainer)
    decide(approver=trainer, request_pk=req.pk, decision="redirect")
    client.force_login(trainer)

    html = client.get(QUEUE).content.decode()

    assert "Told to try again" in html
    assert "redirected" in html


# ---------------------------------------------------------------------------
# The endpoints
# ---------------------------------------------------------------------------


def test_the_decide_endpoint_does_not_collapse_redirect_into_reject(client, trainer) -> None:
    """The view used to compute `approve = decision == "approve"`, which silently
    turned the third option into the second."""
    req = _apply(trainer)
    client.force_login(trainer)

    client.post(
        reverse("accounts:decide", args=[req.pk]),
        {"decision": "redirect"},
        follow=True,
    )

    req.refresh_from_db()
    assert req.status == ApprovalStatus.REDIRECTED


def test_the_undo_endpoints_need_a_post(client, trainer) -> None:
    req = _rejected(trainer)
    client.force_login(trainer)

    assert client.get(reverse("accounts:undo-code", args=[req.pk])).status_code == 405
    assert client.get(reverse("accounts:undo-confirm", args=[req.pk])).status_code == 405


def test_requesting_a_code_by_url_does_not_reveal_the_code(client, trainer) -> None:
    req = _rejected(trainer)
    client.force_login(trainer)

    response = client.post(reverse("accounts:undo-code", args=[req.pk]), follow=True)

    issued_code = ApprovalUndoToken.objects.get(request=req)
    assert issued_code.code_hash
    body = response.content.decode()
    assert "A confirmation code is on its way" in body


def test_redeeming_through_the_endpoint_reverses_the_decline(client, trainer) -> None:
    req = _rejected(trainer)
    issued = request_undo_code(trainer=trainer, request_pk=req.pk)
    client.force_login(trainer)

    client.post(
        reverse("accounts:undo-confirm", args=[req.pk]),
        {"code": issued.code},
        follow=True,
    )

    req.refresh_from_db()
    assert req.status == ApprovalStatus.PENDING


def test_a_trainee_is_refused_the_undo_endpoints(client, trainer) -> None:
    req = _rejected(trainer)
    trainee = make_user("trainee@example.com", role="trainee", approved=True)
    client.force_login(trainee)

    response = client.post(reverse("accounts:undo-code", args=[req.pk]), follow=True)

    assert req.status == ApprovalStatus.REJECTED
    assert "queue" in response.content.decode().lower()


def test_roles_are_untouched_by_an_undo(trainer) -> None:
    """Reversing a decline restores the pending state, not anybody's roles.

    In particular it must not grant the applicant trainer rights: the undo is a
    correction to a decision, and the next approval is what confers anything.
    """
    req = _rejected(trainer)
    before = roles_of(req.user)
    issued = request_undo_code(trainer=trainer, request_pk=req.pk)

    undo_rejection(trainer=trainer, request_pk=req.pk, code=issued.code)

    assert roles_of(req.user) == before
    assert ROLE_ADMIN not in roles_of(req.user)
    assert not req.user.is_superuser

"""The three-way approval decision, and reversing a permanent decline.

Split from `test_views_authz` on purpose: those tests ask "may this person see
the page", these ask "what actually happened to the row". A policy test that
also asserted the resulting status would pass while the service quietly wrote
the wrong value.
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

import pytest
from django.conf import settings
from django.core import mail
from django.urls import reverse
from django.utils import timezone
from django.utils.timezone import now

from apps.accounts.domain.enums import ApprovalStatus
from apps.accounts.models import (
    APPROVAL_UNDO_LIFETIME_MINUTES,
    APPROVAL_UNDO_MAX_ATTEMPTS,
    ROLE_ADMIN,
    ApprovalUndoToken,
    SignupRequest,
)
from apps.accounts.policies import is_approved
from apps.accounts.service.approval import Decision, DecisionError, decide
from apps.accounts.service.signup import submit_application
from apps.accounts.service.undo import UndoError, request_undo_code, undo_rejection
from apps.accounts.tests.conftest import make_user, roles_of

pytestmark = pytest.mark.django_db


QUEUE = reverse("accounts:admin-queue")

#: Markers scoped to the undo page's own form. The dialog in base.html also owns
#: an input called `name="code"`, on every page, so a bare name match would pass
#: for the wrong reason.
PAGE_CODE_FIELD = 'id="code"'



def _apply(requester=None, name="Kiran Rao", email="kiran@example.com", **form):
    """Submit an application the way the public form does, and return the row.

    `requester` is only ever passed by the on-behalf tests; the default
    `None` is a person registering themselves, which is the shape most of this
    file arranges.

    There is no approver argument any more. Under D41 approval is an admin act in
    one shared queue, so the application carries a `requested_role` and nothing
    else -- the deprecated `selected_trainer` is not written by the service and
    this helper would fail if it tried.
    """
    submit_application(
        full_name=name,
        email=email,
        password="Str0ng-Pass!x9",
        requested_role=form.pop("requested_role", "trainee"),
        requester=requester,
    )
    return SignupRequest.objects.get(email=email)


# ---------------------------------------------------------------------------
# The three decisions
# ---------------------------------------------------------------------------


def test_there_are_exactly_three_decisions() -> None:
    assert [d.value for d in Decision] == ["approve", "reject", "redirect"]


def test_approve_moves_the_row_and_the_user_together(admin) -> None:
    req = _apply()

    decide(approver=admin, request_pk=req.pk, decision="approve")

    req.refresh_from_db()
    assert req.status == ApprovalStatus.APPROVED
    assert req.user.approval_status == ApprovalStatus.APPROVED


def test_reject_is_permanent_and_keeps_the_user_inert(admin) -> None:
    req = _apply()

    decide(approver=admin, request_pk=req.pk, decision="reject", note="No.")

    req.refresh_from_db()
    assert req.status == ApprovalStatus.REJECTED
    # Still linked, so the undo can find it again -- but not usable.
    assert req.user.approval_status == ApprovalStatus.REJECTED
    # `is_approved` is the master gate the login view and every policy consult.
    assert not is_approved(req.user)


def test_redirect_closes_the_account_but_leaves_the_address_open(admin) -> None:
    """The distinction that matters: a soft decline must not block re-registering."""
    req = _apply()

    decide(approver=admin, request_pk=req.pk, decision="redirect", note="Wrong admin.")

    req.refresh_from_db()
    assert req.status == ApprovalStatus.REDIRECTED
    assert req.user_id is None, "a soft decline should not leave a dead account behind"


def test_a_redirected_address_may_register_again(admin) -> None:
    _apply()
    first = SignupRequest.objects.get(email="kiran@example.com")
    decide(approver=admin, request_pk=first.pk, decision="redirect")

    second = submit_application(
        full_name="Kiran Rao",
        email="kiran@example.com",
        password="An0ther-Pass!x9",
        requested_role="trainee",
    )

    assert second.request.status == ApprovalStatus.PENDING
    assert SignupRequest.objects.filter(email="kiran@example.com").count() == 2


def test_a_rejected_address_may_not_register_again(admin) -> None:
    req = _apply()
    decide(approver=admin, request_pk=req.pk, decision="reject")

    from apps.accounts.service.signup import SignupError

    with pytest.raises(SignupError):
        submit_application(
            full_name="Kiran Rao",
            email="kiran@example.com",
            password="An0ther-Pass!x9",
            requested_role="trainee",
        )


def test_an_approved_address_may_not_register_again(admin) -> None:
    req = _apply()
    decide(approver=admin, request_pk=req.pk, decision="approve")

    from apps.accounts.service.signup import SignupError

    with pytest.raises(SignupError):
        submit_application(
            full_name="Kiran Rao",
            email="kiran@example.com",
            password="An0ther-Pass!x9",
            requested_role="trainee",
        )


def test_an_unknown_decision_is_refused_rather_than_defaulted(admin) -> None:
    """A typo must not be read as 'approve' or quietly land on some other branch."""
    req = _apply()

    with pytest.raises(DecisionError):
        decide(approver=admin, request_pk=req.pk, decision="aprove")

    req.refresh_from_db()
    assert req.status == ApprovalStatus.PENDING


# ---------------------------------------------------------------------------
# Emails per decision
# ---------------------------------------------------------------------------


def _decision_mail(email: str) -> str:
    return next(m.body for m in mail.outbox if email in m.to)


def test_a_redirect_email_offers_to_register_again(admin) -> None:
    req = _apply()
    decide(approver=admin, request_pk=req.pk, decision="redirect")

    body = _decision_mail("kiran@example.com")
    assert "register again" in body.lower()
    assert reverse("accounts:signup") in _html_for("kiran@example.com")


def test_a_reject_email_says_the_address_is_blocked(admin) -> None:
    req = _apply()
    decide(approver=admin, request_pk=req.pk, decision="reject")

    body = _decision_mail("kiran@example.com")
    assert "cannot register again" in body.lower()


def _html_for(address: str) -> str:
    return next(m.alternatives[0][0] for m in mail.outbox if address in m.to)


# ---------------------------------------------------------------------------
# The undo codes
# ---------------------------------------------------------------------------


def _rejected(admin) -> SignupRequest:
    """A declined application, declined by an admin.

    Only admins can reach this state: under D41 the queue is admin-only, so the
    trainer-centric version of this helper -- where the trainer filed and decided
    -- describes a flow that no longer exists.
    """
    req = _apply()
    decide(approver=admin, request_pk=req.pk, decision="reject", note="Wrong branch.")
    req.refresh_from_db()
    return req


def test_requesting_a_code_emails_the_actor_and_not_the_applicant(admin) -> None:
    req = _rejected(admin)
    mail.outbox.clear()  # the decline itself already mailed the applicant

    issued = request_undo_code(actor=admin, request_pk=req.pk)

    assert issued.code.isdigit() and len(issued.code) == 6
    actor_mail = [m for m in mail.outbox if admin.email in m.to]
    assert actor_mail, "the admin who is undoing gets the code"
    assert issued.code in actor_mail[0].body
    # Copying the applicant would tell them a permanent refusal is being undone.
    assert not [m for m in mail.outbox if "kiran@example.com" in m.to]


def test_the_code_is_stored_hashed_not_in_the_clear(admin) -> None:
    req = _rejected(admin)

    issued = request_undo_code(actor=admin, request_pk=req.pk)

    stored = ApprovalUndoToken.objects.get(request=req)
    assert issued.code not in stored.code_hash
    assert stored.code_hash != issued.code


def test_a_valid_code_returns_the_row_to_pending(admin) -> None:
    req = _rejected(admin)
    issued = request_undo_code(actor=admin, request_pk=req.pk)

    undo_rejection(actor=admin, request_pk=req.pk, code=issued.code)

    req.refresh_from_db()
    assert req.status == ApprovalStatus.PENDING
    assert req.user.approval_status == ApprovalStatus.PENDING


def test_reversing_tells_the_applicant_and_names_who_did_it(admin) -> None:
    req = _rejected(admin)
    issued = request_undo_code(actor=admin, request_pk=req.pk)
    mail.outbox.clear()

    undo_rejection(actor=admin, request_pk=req.pk, code=issued.code)

    body = _decision_mail("kiran@example.com")
    assert "reversed" in body.lower()
    assert admin.full_name in body


def test_a_code_cannot_be_redeemed_twice(admin) -> None:
    req = _rejected(admin)
    issued = request_undo_code(actor=admin, request_pk=req.pk)
    undo_rejection(actor=admin, request_pk=req.pk, code=issued.code)

    with pytest.raises(UndoError):
        undo_rejection(actor=admin, request_pk=req.pk, code=issued.code)


def test_a_wrong_code_is_refused_and_counted(admin) -> None:
    req = _rejected(admin)
    request_undo_code(actor=admin, request_pk=req.pk)

    with pytest.raises(UndoError):
        undo_rejection(actor=admin, request_pk=req.pk, code="000000")

    assert ApprovalUndoToken.objects.get(request=req).attempts == 1
    req.refresh_from_db()
    assert req.status == ApprovalStatus.REJECTED, "a wrong guess must not move the row"


def test_guessing_is_capped_so_the_code_cannot_be_brute_forced(admin) -> None:
    req = _rejected(admin)
    request_undo_code(actor=admin, request_pk=req.pk)

    for _ in range(APPROVAL_UNDO_MAX_ATTEMPTS):
        with pytest.raises(UndoError):
            undo_rejection(actor=admin, request_pk=req.pk, code="111111")

    # A further attempt must not even be hashed and compared.
    with pytest.raises(UndoError, match="Too many"):
        undo_rejection(actor=admin, request_pk=req.pk, code="222222")


def test_an_expired_code_is_refused(admin) -> None:
    from datetime import timedelta

    from django.utils import timezone

    req = _rejected(admin)
    issued = request_undo_code(actor=admin, request_pk=req.pk)
    ApprovalUndoToken.objects.filter(request=req).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )

    with pytest.raises(UndoError, match="expired"):
        undo_rejection(actor=admin, request_pk=req.pk, code=issued.code)


def test_asking_again_invalidates_the_first_code(admin) -> None:
    """An abandoned code must not stay live in an old inbox."""
    req = _rejected(admin)
    first = request_undo_code(actor=admin, request_pk=req.pk)
    second = request_undo_code(actor=admin, request_pk=req.pk)

    with pytest.raises(UndoError):
        undo_rejection(actor=admin, request_pk=req.pk, code=first.code)

    undo_rejection(actor=admin, request_pk=req.pk, code=second.code)
    req.refresh_from_db()
    assert req.status == ApprovalStatus.PENDING


# ---------------------------------------------------------------------------
# Who may undo
# ---------------------------------------------------------------------------


def test_a_trainer_cannot_undo(admin, trainer) -> None:
    """Under D41 undo is an admin act, exactly like the decision it reverses.

    The old version of this test asserted that a *different* trainer could not
    undo their colleague's rejection, which was the per-trainer split. There is no
    split now: one queue, one role, so the meaningful assertion is the stronger
    one -- no trainer may undo at all, their own included.
    """
    req = _rejected(admin)

    with pytest.raises(UndoError):
        request_undo_code(actor=trainer, request_pk=req.pk)


def test_any_admin_may_undo_any_rejection(admin) -> None:
    req = _rejected(admin)
    other_admin = make_user("other.admin@example.test", role=ROLE_ADMIN)
    issued = request_undo_code(actor=other_admin, request_pk=req.pk)

    undo_rejection(actor=other_admin, request_pk=req.pk, code=issued.code)

    req.refresh_from_db()
    assert req.status == ApprovalStatus.PENDING


def test_a_superuser_may_undo(admin) -> None:
    """The break-glass override, honoured so whoever is on call is not locked out.

    `createsuperuser` grants no RoleAssignment, so without this the person who can
    edit users in /admin/ would 404 on a queue they outrank everyone on.
    """
    req = _rejected(admin)
    operator = make_user("operator@example.test", approved=True)
    operator.is_superuser = True
    operator.save(update_fields=["is_superuser"])

    issued = request_undo_code(actor=operator, request_pk=req.pk)

    assert issued.code.isdigit()


def test_a_pending_row_offers_no_undo(admin) -> None:
    req = _apply()

    with pytest.raises(UndoError):
        request_undo_code(actor=admin, request_pk=req.pk)


def test_an_admin_cannot_undo_their_own_application(admin) -> None:
    """Separation of duties applies to reversing a decision too.

    Otherwise an admin could have their own application declined and immediately
    undo that decline. `can_approve` already refuses the decision itself; the undo
    must refuse it too rather than leaning on a later approval step to catch it.
    """
    # Not reachable through the public form: signup refuses an address that already
    # has an account. The guard is defence in depth, mirroring `can_approve`, and is
    # exercised here by writing the row directly rather than pretending a user
    # journey reaches it.
    req = SignupRequest.objects.create(
        full_name=admin.full_name,
        email=admin.email,
        user=admin,
        status=ApprovalStatus.PENDING,
    )
    SignupRequest.objects.filter(pk=req.pk).update(status=ApprovalStatus.REJECTED)
    assert req.user_id == admin.pk

    with pytest.raises(UndoError, match="your own"):
        request_undo_code(actor=admin, request_pk=req.pk)


def test_a_trainee_cannot_undo(admin) -> None:
    req = _rejected(admin)
    trainee = make_user("trainee@example.com", role="trainee", approved=True)

    with pytest.raises(UndoError):
        request_undo_code(actor=trainee, request_pk=req.pk)


# ---------------------------------------------------------------------------
# The queue page
# ---------------------------------------------------------------------------


def test_the_queue_offers_all_three_decisions(client, admin) -> None:
    _apply()
    client.force_login(admin)

    html = client.get(QUEUE).content.decode()

    assert 'value="approve"' in html
    assert 'value="reject"' in html
    assert 'value="redirect"' in html


def test_the_undo_form_appears_only_on_a_rejected_row(client, admin) -> None:
    pending = _apply(email="pending@example.com")
    rejected = _rejected(admin)
    client.force_login(admin)

    on_pending = client.get(QUEUE).content.decode()
    on_rejected = client.get(QUEUE, {"filter": "rejected"}).content.decode()

    # Nothing to undo while the application is still awaiting a decision...
    assert reverse("accounts:undo-code", args=[pending.pk]) not in on_pending
    assert reverse("accounts:undo-confirm", args=[pending.pk]) not in on_pending
    # ...and the undo offered once it has been declined.
    assert reverse("accounts:undo-code", args=[rejected.pk]) in on_rejected
    assert reverse("accounts:undo-confirm", args=[rejected.pk]) in on_rejected


def test_a_declined_row_offers_exactly_one_way_to_undo(client, admin) -> None:
    """The row must be a single button.

    It used to render a button to send a code *and*, beside it, a permanently
    visible box captioned "Or enter the code we emailed you". That asked the
    admin to choose between two things which are consecutive steps, and let
    them submit an empty box before any code existed.
    """
    req = _rejected(admin)
    client.force_login(admin)

    html = client.get(QUEUE, {"filter": "rejected"}).content.decode()

    assert html.count("data-undo-start") == 1
    # One form, pointing at the send endpoint. The confirm URL is present only as
    # the button's data-confirm-url, which is how the dialog knows where to post.
    confirm_url = reverse("accounts:undo-confirm", args=[req.pk])
    assert confirm_url in html
    assert f'action="{confirm_url}"' not in html
    # And no code box of any kind on the row.
    assert "queue__undo-form" not in html
    # Scoped to href/action: the standalone page URL is a prefix of .../undo/code/,
    # which the form legitimately posts to.
    page_url = reverse("accounts:undo-request", args=[req.pk])
    assert f'href="{page_url}"' not in html
    assert f'action="{page_url}"' not in html


def test_the_code_box_lives_on_its_own_page_for_no_js(client, admin) -> None:
    """The queue row has one button; the no-JS path gets a page of its own rather
    than a second control competing with it."""
    req = _rejected(admin)
    client.force_login(admin)

    page = reverse("accounts:undo-request", args=[req.pk])

    # Before a code exists the page asks for one...
    first = client.get(page).content.decode()
    assert "Email me a code" in first
    assert PAGE_CODE_FIELD not in first

    request_undo_code(actor=admin, request_pk=req.pk)

    # ...and afterwards it asks for the code, not for another one.
    second = client.get(page).content.decode()
    assert PAGE_CODE_FIELD in second
    assert "Email me a code" not in second


def test_the_undo_page_is_closed_to_anybody_who_cannot_undo(client, admin) -> None:
    """Nobody who is not an admin gets the page, whatever their role."""
    req = _rejected(admin)
    trainee = make_user("trainee@example.com", role="trainee", approved=True)
    other = make_user("nosy@example.com", role="trainer", approved=True)
    client.force_login(trainee)
    assert client.get(reverse("accounts:undo-request", args=[req.pk])).status_code == 404

    # A trainer is 404 too now: undo moved into the admin-only queue with the
    # decision it reverses.
    client.force_login(other)
    assert client.get(reverse("accounts:undo-request", args=[req.pk])).status_code == 404


def test_a_pending_application_has_no_undo_page(client, admin) -> None:
    pending = _apply(email="pending@example.com")
    client.force_login(admin)

    response = client.get(reverse("accounts:undo-request", args=[pending.pk]))

    assert response.status_code == 404


def test_a_sent_code_tells_the_button_to_skip_ahead(client, admin) -> None:
    """With a code already out, the dialog opens on the code box.

    Mailing a second one would expire the first, which the admin would read as
    a bad code rather than as their own earlier click.
    """
    req = _rejected(admin)
    client.force_login(admin)

    before = client.get(QUEUE, {"filter": "rejected"}).content.decode()
    assert "data-undo-pending" not in before

    request_undo_code(actor=admin, request_pk=req.pk)

    after = client.get(QUEUE, {"filter": "rejected"}).content.decode()
    assert "data-undo-pending" in after


def test_an_expired_code_does_not_promise_a_way_in(client, admin) -> None:
    """A live code is what unlocks the box. An expired one must not, or the
    admin is offered a way to finish that cannot succeed."""
    req = _rejected(admin)
    request_undo_code(actor=admin, request_pk=req.pk)
    ApprovalUndoToken.objects.filter(request=req).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    client.force_login(admin)

    queue_html = client.get(QUEUE, {"filter": "rejected"}).content.decode()
    page_html = client.get(reverse("accounts:undo-request", args=[req.pk])).content.decode()

    assert "data-undo-pending" not in queue_html
    assert "Email me a code" in page_html


def test_a_trainer_never_sees_the_queue_at_all(client, admin, trainer) -> None:
    """The shared queue has one reader role, so the whole page is out of reach.

    This replaced a test asserting that one trainer could not see *another
    trainer's* row. Under D41 that per-trainer boundary is gone along with the
    split, and asserting it would pin behaviour nobody wants back.
    """
    # Arranged but unused by name: the row must exist so that a 404 is the
    # *permission* refusal rather than an empty queue that happens to 404 too.
    _rejected(admin)
    client.force_login(trainer)

    response = client.get(QUEUE, {"filter": "rejected"})

    assert response.status_code == 404


def test_a_redirected_row_gets_its_own_tab(client, admin) -> None:
    req = _apply()
    decide(approver=admin, request_pk=req.pk, decision="redirect")
    client.force_login(admin)

    html = client.get(QUEUE).content.decode()

    assert "Told to try again" in html
    assert "redirected" in html


# ---------------------------------------------------------------------------
# The endpoints
# ---------------------------------------------------------------------------


def test_the_decide_endpoint_does_not_collapse_redirect_into_reject(client, admin) -> None:
    """The view used to compute `approve = decision == "approve"`, which silently
    turned the third option into the second."""
    req = _apply()
    client.force_login(admin)

    client.post(
        reverse("accounts:decide", args=[req.pk]),
        {"decision": "redirect"},
        follow=True,
    )

    req.refresh_from_db()
    assert req.status == ApprovalStatus.REDIRECTED


def test_the_undo_endpoints_need_a_post(client, admin) -> None:
    req = _rejected(admin)
    client.force_login(admin)

    assert client.get(reverse("accounts:undo-code", args=[req.pk])).status_code == 405
    assert client.get(reverse("accounts:undo-confirm", args=[req.pk])).status_code == 405


def test_requesting_a_code_by_url_does_not_reveal_the_code(client, admin) -> None:
    req = _rejected(admin)
    client.force_login(admin)

    response = client.post(reverse("accounts:undo-code", args=[req.pk]), follow=True)

    issued_code = ApprovalUndoToken.objects.get(request=req)
    assert issued_code.code_hash
    body = response.content.decode()
    assert "A confirmation code is on its way" in body


def test_redeeming_through_the_endpoint_reverses_the_decline(client, admin) -> None:
    req = _rejected(admin)
    issued = request_undo_code(actor=admin, request_pk=req.pk)
    client.force_login(admin)

    client.post(
        reverse("accounts:undo-confirm", args=[req.pk]),
        {"code": issued.code},
        follow=True,
    )

    req.refresh_from_db()
    assert req.status == ApprovalStatus.PENDING


def test_a_trainee_is_refused_the_undo_endpoints(client, admin) -> None:
    req = _rejected(admin)
    trainee = make_user("trainee@example.com", role="trainee", approved=True)
    client.force_login(trainee)

    response = client.post(reverse("accounts:undo-code", args=[req.pk]), follow=True)

    assert req.status == ApprovalStatus.REJECTED
    assert "queue" in response.content.decode().lower()


# ---------------------------------------------------------------------------
# The dialog's JSON contract
#
# The undo dialog posts with X-Requested-With and reads JSON, while the no-JS
# fallback posts the same endpoints as a plain form. Both shapes come from one
# service error, so these tests pin the agreement.
# ---------------------------------------------------------------------------


def test_redeeming_answers_the_dialog_with_the_new_state(client, admin) -> None:
    req = _rejected(admin)
    issued = request_undo_code(actor=admin, request_pk=req.pk)
    client.force_login(admin)

    response = client.post(
        reverse("accounts:undo-confirm", args=[req.pk]),
        {"code": issued.code},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert response.status_code == 200
    body = response.json()
    assert body == {"ok": True, "full_name": req.full_name, "status": "pending"}


def test_a_wrong_code_answers_the_dialog_and_changes_nothing(client, admin) -> None:
    """The dialog's whole error path: report it, stay put.

    The admin must still be looking at a declined application, because that is
    what the service left behind.
    """
    req = _rejected(admin)
    request_undo_code(actor=admin, request_pk=req.pk)
    client.force_login(admin)

    response = client.post(
        reverse("accounts:undo-confirm", args=[req.pk]),
        {"code": "000000"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert response.status_code == 400
    body = response.json()
    assert body["ok"] is False
    assert body["error"]
    req.refresh_from_db()
    assert req.status == ApprovalStatus.REJECTED


def test_a_failed_send_leaves_the_row_declined(client, admin) -> None:
    """Sending a code is not a decision, so a refusal must not move the row."""
    req = _rejected(admin)
    admin.is_superuser = True
    admin.save()
    # Now the request has no linked user to restore, so the send is refused.
    req.user = None
    req.save()
    client.force_login(admin)

    response = client.post(
        reverse("accounts:undo-code", args=[req.pk]),
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert response.status_code == 400
    assert response.json()["ok"] is False
    req.refresh_from_db()
    assert req.status == ApprovalStatus.REJECTED


def test_the_endpoints_still_serve_plain_form_posts(client, admin) -> None:
    """The no-JS path must keep working: no AJAX header, redirect not JSON."""
    req = _rejected(admin)
    issued = request_undo_code(actor=admin, request_pk=req.pk)
    client.force_login(admin)

    # The send lands on the undo page, where the code can still be entered.
    plain = client.post(reverse("accounts:undo-code", args=[req.pk]))
    assert plain.status_code == 302
    assert plain["Location"] == reverse("accounts:undo-request", args=[req.pk])

    ok = client.post(reverse("accounts:undo-confirm", args=[req.pk]), {"code": issued.code})
    assert ok.status_code == 302
    assert ok["Location"] == reverse("accounts:admin-queue")


def test_a_refusal_answers_the_dialog_with_a_reason_not_a_redirect(client, admin) -> None:
    """A redirect would reach the dialog as unparseable HTML and read as
    "unexpected reply" instead of the real reason. The status is the service's
    to choose; the shape is not."""
    req = _rejected(admin)
    trainee = make_user("trainee@example.com", role="trainee", approved=True)
    client.force_login(trainee)

    response = client.post(
        reverse("accounts:undo-code", args=[req.pk]),
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert response.status_code == 400
    assert response["Content-Type"].startswith("application/json")
    body = response.json()
    assert body["ok"] is False
    assert body["error"]
    req.refresh_from_db()
    assert req.status == ApprovalStatus.REJECTED


def test_roles_are_untouched_by_an_undo(admin) -> None:
    """Reversing a decline restores the pending state, not anybody's roles.

    In particular it must not grant the applicant trainer rights: the undo is a
    correction to a decision, and the next approval is what confers anything.
    """
    req = _rejected(admin)
    before = roles_of(req.user)
    issued = request_undo_code(actor=admin, request_pk=req.pk)

    undo_rejection(actor=admin, request_pk=req.pk, code=issued.code)

    assert roles_of(req.user) == before
    assert ROLE_ADMIN not in roles_of(req.user)
    assert not req.user.is_superuser


def _usable_token(req):
    """A token that could still be redeemed: unconsumed and unexpired."""
    return ApprovalUndoToken.objects.filter(
        request=req, consumed_at__isnull=True, expires_at__gt=timezone.now()
    ).exists()


# ---------------------------------------------------------------------------
# "Code sent" has to mean sent.
#
# The panel used to advance the moment `.delay()` returned, which only proves the
# broker took the message. A worker running code from before these tasks existed
# discarded such a message without a word, so the trainer was told to check an
# inbox that was never going to hear anything -- with no way to tell that apart
# from their own mistake. The send is now synchronous, so a delivery failure is a
# refusal the panel can show. These tests pin that.
# ---------------------------------------------------------------------------


def test_requesting_a_code_answers_with_a_delivery_receipt(client, admin) -> None:
    req = _rejected(admin)
    client.force_login(admin)

    response = client.post(
        reverse("accounts:undo-code", args=[req.pk]),
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    # `sent` is a receipt, not a hope: the mail left inside this request, so a
    # failure would have come back as `ok: false` and never reached here.
    assert body["sent"] is True
    assert body["email"] == admin.email
    # The countdown is seeded from the token's real remaining life.
    assert 0 < body["seconds_left"] <= APPROVAL_UNDO_LIFETIME_MINUTES * 60
    # Never the code itself -- it goes in the email, not the response body.
    assert "code" not in body


def test_the_token_records_that_the_code_was_mailed(client, admin) -> None:
    """A delivery leaves an audit trail, so support can answer "did we email it?"."""
    req = _rejected(admin)
    client.force_login(admin)

    issued = request_undo_code(actor=admin, request_pk=req.pk)
    # The stamp is written with a queryset update, so the instance handed back by
    # the service is deliberately not re-read; the row is what matters.
    issued.token.refresh_from_db()

    assert issued.token.emailed_at is not None
    assert issued.token.emailed_at <= timezone.now()


def test_a_mail_server_failure_is_refused_not_reported_as_sent(client, admin) -> None:
    """The whole point of sending inline: a broken SMTP server has to surface."""
    req = _rejected(admin)
    client.force_login(admin)

    with mock.patch(
        "apps.accounts.tasks.deliver_undo_code", side_effect=OSError("smtp down")
    ):
        response = client.post(
            reverse("accounts:undo-code", args=[req.pk]),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

    body = response.json()
    assert body["ok"] is False
    assert "could not send" in body["error"]
    # Nothing is claimed, and nothing is left half-open.
    assert "sent" not in body
    req.refresh_from_db()
    assert req.status == ApprovalStatus.REJECTED
    # Nothing left that could be attempted. The row itself is kept as the record
    # that a code was asked for; it is expired, so the server would refuse it.
    assert not _usable_token(req)


def test_a_mail_server_that_sends_nothing_is_also_refused(client, admin) -> None:
    """`send_mail` returning 0 means no message went out. That is a failure."""
    req = _rejected(admin)
    client.force_login(admin)

    with mock.patch("apps.accounts.tasks.deliver_undo_code", return_value=0):
        body = client.post(
            reverse("accounts:undo-code", args=[req.pk]),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        ).json()

    assert body["ok"] is False
    assert "could not send" in body["error"]
    assert not _usable_token(req)


def test_the_send_failure_is_logged_in_full_not_shown_to_the_trainer(
    client, admin, caplog
) -> None:
    """The admin gets a plain sentence; the cause stays server-side."""
    req = _rejected(admin)
    client.force_login(admin)

    with mock.patch(
        "apps.accounts.tasks.deliver_undo_code", side_effect=OSError("auth failed")
    ), caplog.at_level("ERROR", logger="apps.accounts.service.undo"):
        body = client.post(
            reverse("accounts:undo-code", args=[req.pk]),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        ).json()

    assert "auth failed" not in body["error"]
    assert "auth failed" in caplog.text


def test_a_live_code_puts_the_box_on_the_no_js_page(client, admin) -> None:
    req = _rejected(admin)
    client.force_login(admin)

    request_undo_code(actor=admin, request_pk=req.pk)

    page = client.get(reverse("accounts:undo-request", args=[req.pk])).content.decode()

    assert 'id="code"' in page
    assert "Email me a code again" not in page


def test_an_incorrect_code_reads_as_an_incorrect_code(client, admin) -> None:
    """The wording names the input problem, not the person or the attempt."""
    req = _rejected(admin)
    client.force_login(admin)
    issued = request_undo_code(actor=admin, request_pk=req.pk)

    body = client.post(
        reverse("accounts:undo-confirm", args=[req.pk]),
        {"code": "000000" if issued.code != "000000" else "111111"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    ).json()

    assert body["ok"] is False
    assert body["error"] == "Incorrect code. Try again."
    # And the refusal did not quietly become a decision.
    req.refresh_from_db()
    assert req.status == ApprovalStatus.REJECTED


def test_the_last_wrong_code_says_there_are_no_attempts_left(client, admin) -> None:
    req = _rejected(admin)
    client.force_login(admin)
    issued = request_undo_code(actor=admin, request_pk=req.pk)

    ApprovalUndoToken.objects.filter(pk=issued.token.pk).update(
        attempts=APPROVAL_UNDO_MAX_ATTEMPTS - 1
    )
    body = client.post(
        reverse("accounts:undo-confirm", args=[req.pk]),
        {"code": "000000"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    ).json()

    assert body["error"] == "Incorrect code. No attempts left -- request a new one."


def test_the_panel_is_marked_up_as_a_top_centre_panel_with_a_clock(client, admin) -> None:
    """The panel is positioned and animated like a toast, not like a modal dialog.

    It has to be one of the two by construction, not by coincidence: a centred
    dialog covers the queue the admin is working through, and a bare toast would
    auto-dismiss the only input on the page.
    """
    _rejected(admin)
    client.force_login(admin)

    html = client.get(QUEUE, {"filter": "rejected"}).content.decode()

    assert 'id="undo-otp"' in html
    assert 'class="otp__panel"' in html
    # A <dialog> would be centred and modal; this is neither.
    assert 'id="undo-dialog"' not in html
    assert "aria-modal" not in html
    # The clock the countdown drives, and the digits it goes with.
    assert 'id="undo-clock"' in html
    assert 'id="undo-code-input"' in html


def test_the_undo_panel_reuses_the_toast_arrival(client, admin) -> None:
    """One motion for feedback on this page, not two that disagree."""
    # Located from settings rather than relative to this test, so the path does not
    # depend on how deep the test file sits.
    css = settings.BASE_DIR / "src" / "frontend" / "static" / "css" / "avanyam.css"

    text = css.read_text()
    assert ".js .otp__panel { animation: toast-in" in text


def test_the_button_carries_the_address_so_the_in_flight_state_can_name_it(
    client, admin
) -> None:
    """The panel says where the code is going before the server has answered.

    Naming the address in the "Sending your code" state needs it on the button, and
    getting it from the response would mean the admin reads a blank line for the
    several seconds the SMTP handshake takes.
    """
    _rejected(admin)
    client.force_login(admin)

    html = client.get(QUEUE, {"filter": "rejected"}).content.decode()

    assert f'data-email="{admin.email}"' in html
    assert 'data-undo-start' in html


def test_the_panel_reports_a_send_in_flight_rather_than_freezing(client, admin) -> None:
    """There is a visible in-between state, because the send really does take seconds."""
    js = settings.BASE_DIR / "src" / "frontend" / "static" / "js" / "portal.js"

    text = js.read_text()
    assert '"sending"' in text
    # The state is entered before the request leaves, not after it returns.
    send_at = text.index("setStep(\"sending\");")
    # The call site, not the `function post(url, payload)` declaration above it.
    post_at = text.index("post(url, step ===")
    assert send_at < post_at
    assert "Sending your code" in text


# ---------------------------------------------------------------------------
# Coming back to a code that is already in the trainer's inbox
#
# A trainer who asked for a code, then navigated away and came back, is holding a
# code that is still valid. The queue has to acknowledge that rather than ask them
# for another one, because a second code expires the first -- which looks exactly
# like a wrong code and burns an attempt.
# ---------------------------------------------------------------------------


def _flat(html: str) -> str:
    """Collapse whitespace so assertions survive hand-wrapped template output."""
    return " ".join(html.split())


#: Deliberately not the address `test_observability`'s fixture uses. The suite runs
#: against a long-lived local test database, so a name shared across files turns any
#: stray row into a confusing failure somewhere else entirely.
QUEUE_CODE_EMAIL = "queue.countdown@example.com"


def _declined_with_live_code(admin):
    """A declined application with a live, delivered code waiting."""
    req = _apply(name="Meera Iyer", email=QUEUE_CODE_EMAIL)
    decide(approver=admin, request_pk=req.pk, decision="reject", note="Wrong branch.")
    request_undo_code(actor=admin, request_pk=req.pk)
    return SignupRequest.objects.get(pk=req.pk)


def test_a_live_code_puts_the_queue_row_into_the_code_box(client, admin) -> None:
    _declined_with_live_code(admin)
    client.force_login(admin)

    html = _flat(client.get(QUEUE, {"filter": "rejected"}).content.decode())

    # The row must offer the code box, not the send button.
    assert "data-undo-pending" in html
    assert "Enter the emailed code" in html
    assert "Undo decline </button>" not in html


def test_the_queue_countdown_starts_from_the_token_not_from_the_full_window(
    client, admin
) -> None:
    """A page left open must not offer more time than the token actually has.

    The button reads "Enter the emailed code", so it is promising the admin a
    code they can still use. If the countdown restarted at the full window, a
    queue opened eight minutes in would show a fresh ten and then reject a correct
    code as expired.
    """
    req = _declined_with_live_code(admin)
    token = ApprovalUndoToken.objects.get(request=req)
    token.expires_at = now() + timedelta(seconds=90)
    token.save(update_fields=["expires_at"])
    client.force_login(admin)

    html = client.get(QUEUE, {"filter": "rejected"}).content.decode()

    remaining = int(html.split('data-seconds-left="')[1].split('"')[0])
    assert 0 < remaining <= 90
    full = APPROVAL_UNDO_LIFETIME_MINUTES * 60
    assert remaining < full, f"countdown restarted at the full window ({remaining}s)"


def test_an_expired_code_puts_the_row_back_to_sending_a_fresh_one(client, admin) -> None:
    """Once the code is gone there is nothing to enter, so the row must offer a new one."""
    req = _declined_with_live_code(admin)
    ApprovalUndoToken.objects.filter(request=req).update(expires_at=now() - timedelta(seconds=1))
    client.force_login(admin)

    html = _flat(client.get(QUEUE, {"filter": "rejected"}).content.decode())

    assert "data-undo-pending" not in html
    assert "Undo decline </button>" in html


def test_a_code_requested_by_someone_else_is_not_offered(client, admin) -> None:
    """The pending marker is per-viewer: a colleague's code is not this admin's."""
    req = _declined_with_live_code(admin)
    other = make_user("other.admin@example.test", role=ROLE_ADMIN)
    token = ApprovalUndoToken.objects.get(request=req)
    ApprovalUndoToken.objects.filter(pk=token.pk).update(requested_by=other)
    client.force_login(admin)

    html = client.get(QUEUE, {"filter": "rejected"}).content.decode()

    assert "data-undo-pending" not in html


def test_the_no_js_page_reports_the_real_remaining_minutes(client, admin) -> None:
    """Same drift, same fix: the page must not promise the full window again."""
    req = _declined_with_live_code(admin)
    token = ApprovalUndoToken.objects.get(request=req)
    token.expires_at = now() + timedelta(seconds=100)
    token.save(update_fields=["expires_at"])
    client.force_login(admin)

    html = _flat(client.get(reverse("accounts:undo-request", args=[req.pk])).content.decode())

    assert PAGE_CODE_FIELD in html, "should be the code box, not the send button"
    assert "Valid for 2 more minutes." in html
    assert f"Valid for {APPROVAL_UNDO_LIFETIME_MINUTES} minutes" not in html


def test_the_dialog_opens_on_the_code_box_when_a_code_is_waiting() -> None:
    """The JS half of the same rule, pinned on the source.

    `open()` used to call `setStep("send")` unconditionally, so the button labelled
    "Enter the emailed code" opened a panel asking to email a code -- and submitting
    that minted a second one, expiring the one the admin was holding.
    """
    js = settings.BASE_DIR / "src" / "frontend" / "static" / "js" / "portal.js"
    text = js.read_text()

    body = text[text.index("function open(button)") : text.index("function post(url")]
    assert "data-undo-pending" in body
    # The pending branch has to come before the unconditional fallback.
    assert body.index('setStep("code")') < body.index('setStep("send")')
    assert 'getAttribute("data-seconds-left")' in body
    # The countdown strip renders "Sent to <address>". The send path fills the
    # address in from the response; this branch has to fill it too, or the strip
    # reads "Sent to" with nothing after it.
    assert "emailOut.textContent" in body

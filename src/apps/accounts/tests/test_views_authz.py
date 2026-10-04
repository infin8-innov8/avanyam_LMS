"""View-level authorization: the HTTP trust boundary.

`test_policies.py` proves the rules are right and `test_approval_flow.py` proves
the service enforces them. Neither proves the *views* route every request through
them. `views.py` opens by claiming "Every authorization question is delegated to
policies (§7). No view inspects `user.is_trainer` directly" -- these tests are
what makes that claim true rather than aspirational.

The important case is the IDOR one: application ids are UUIDs, which people assume
are unguessable. They are not a security control. A trainer who learns another
trainer's applicant UUID -- from a URL, a referrer, a screenshot, a former
colleague -- must not be able to decide that application by POSTing to it.
"""

from __future__ import annotations
import re
from pathlib import Path

import pytest
from django.urls import reverse

from apps.accounts.models import ROLE_TRAINEE, ROLE_TRAINER, SignupRequest
from .conftest import GOOD_PASSWORD, make_user

pytestmark = pytest.mark.django_db

QUEUE_URL = "/accounts/trainer/queue/"


def decide_url(req: SignupRequest) -> str:
    return reverse("accounts:decide", args=[req.pk])


def an_application_for(trainer, *, decided: str | None = None) -> SignupRequest:
    applicant = make_user(
        f"app.{trainer.email.split('@')[0]}.{abs(hash(trainer.pk)) % 9973}@example.test",
        role=ROLE_TRAINEE,
        approved=False,
    )
    return SignupRequest.objects.create(
        email=applicant.email,
        full_name=applicant.full_name,
        user=applicant,
        selected_trainer=trainer,
        status=decided or "pending",
    )


# ---------------------------------------------------------------------------
# Anonymous callers get nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "/accounts/pending/",
        QUEUE_URL,
        "/accounts/password/",
    ],
)
def test_anonymous_is_redirected_to_login(client, url: str) -> None:
    response = client.get(url)
    assert response.status_code == 302
    assert "/accounts/login/" in response["Location"]


def test_anonymous_cannot_decide(client, trainer) -> None:
    req = an_application_for(trainer)
    response = client.post(decide_url(req), {"decision": "approve"})
    assert response.status_code == 302
    assert "/accounts/login/" in response["Location"]
    req.refresh_from_db()
    assert req.status == "pending", "anonymous POST changed application state"


# ---------------------------------------------------------------------------
# Non-approvers get 404, not 403
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", [QUEUE_URL])
def test_trainee_has_no_queue(client, approved_trainee, url: str) -> None:
    """404 rather than 403: a 403 would confirm the queue exists."""
    client.force_login(approved_trainee)
    assert client.get(url).status_code == 404


def test_pending_user_has_no_queue(client, trainee) -> None:
    client.force_login(trainee)
    assert client.get(QUEUE_URL).status_code == 404


def test_inactive_trainer_loses_access_entirely(client, trainer) -> None:
    """Django refuses to resolve an inactive user, so the session stops working.

    This is a 302 to login rather than the 404 a signed-in non-approver gets,
    because `get_user()` returns AnonymousUser for a deactivated account. Worth
    pinning: "deactivated means logged out" and "not an approver" are different
    mechanisms and should not be confused.
    """
    trainer.is_active = False
    trainer.save(update_fields=["is_active"])
    client.force_login(trainer)

    response = client.get(QUEUE_URL)

    assert response.status_code == 302
    assert "/accounts/login/" in response["Location"]


def test_approved_trainer_has_a_queue(client, trainer) -> None:
    client.force_login(trainer)
    assert client.get(QUEUE_URL).status_code == 200


def test_trainer_with_nothing_pending_gets_an_empty_queue_not_an_error(
    client, trainer
) -> None:
    """A trainer with no applications must get the empty state, not a 404.

    This is the screen a trainer is meant to see between requests. It used to be
    untested -- the only assertion was a bare 200 -- so "no requests" and "not
    authorised" were indistinguishable from the outside, and the refusal page
    read like a broken link.
    """
    client.force_login(trainer)

    response = client.get(QUEUE_URL)
    body = response.content.decode()

    assert response.status_code == 200
    assert "Nothing waiting on you" in body
    # The queue chrome is present too, so it is recognisably the approvals page.
    assert "Registration requests" in body


def test_non_approver_gets_a_readable_page_not_a_debug_traceback(
    client, approved_trainee
) -> None:
    """Still 404 -- the status is the security control, not the cosmetics.

    What is pinned here is the *body*: an explanation plus a working link out.
    Django renders its own technical 404 whenever DEBUG is on and ignores
    404.html, so this had to be rendered by the view.
    """
    client.force_login(approved_trainee)

    response = client.get(QUEUE_URL)
    body = response.content.decode()

    assert response.status_code == 404
    assert "No approval queue for this account" in body
    assert "Page not found" not in body
    assert "Traceback" not in body
    # No dead end: there is a way onward from the refusal.
    assert "/accounts/pending/" in body


def test_refusal_page_explains_a_pending_registration(client, trainee) -> None:
    """The applicant most likely to hit this gets told what is actually happening."""
    client.force_login(trainee)

    body = client.get(QUEUE_URL).content.decode()

    assert "still pending" in body


# ---------------------------------------------------------------------------
# The IDOR case
# ---------------------------------------------------------------------------


def test_trainer_cannot_decide_another_trainers_applicant_by_uuid(client, trainer, other_trainer):
    """A valid session, a real UUID, the wrong trainer.

    The UUID is not the control here -- `decide()` must refuse on identity. What
    matters is that the row is untouched and the refusal is not silently ignored.
    """
    victim = an_application_for(trainer)
    client.force_login(other_trainer)

    response = client.post(decide_url(victim), {"decision": "approve"})

    victim.refresh_from_db()
    assert victim.status == "pending", "another trainer's application was decided"
    assert response.status_code == 302
    assert response["Location"] == QUEUE_URL


def test_the_decision_also_fails_at_the_service_layer(client, trainer, other_trainer):
    """Belt and braces: the view is not the only thing refusing."""
    from apps.accounts.service.approval import DecisionError, decide

    victim = an_application_for(trainer)
    with pytest.raises(DecisionError):
        decide(approver=other_trainer, request_pk=victim.pk, approve=True)
    victim.refresh_from_db()
    assert victim.status == "pending"


def test_chosen_trainer_can_decide(client, trainer) -> None:
    req = an_application_for(trainer)
    client.force_login(trainer)

    response = client.post(decide_url(req), {"decision": "approve"})

    req.refresh_from_db()
    assert req.status == "approved"
    assert response.status_code == 302


def test_queue_only_lists_your_own_applicants(client, trainer, other_trainer) -> None:
    """Not just undecidable -- invisible."""
    mine = an_application_for(trainer)
    theirs = an_application_for(other_trainer)

    client.force_login(trainer)
    body = client.get(QUEUE_URL).content.decode()

    assert mine.full_name in body
    assert theirs.full_name not in body


def test_admin_can_decide_an_unassigned_application(client, admin) -> None:
    """The admin path: an application with no trainer is otherwise stranded."""
    applicant = make_user("stranded@example.test", role=ROLE_TRAINEE, approved=False)
    req = SignupRequest.objects.create(
        email=applicant.email, full_name=applicant.full_name, user=applicant, selected_trainer=None
    )
    client.force_login(admin)

    response = client.post(decide_url(req), {"decision": "approve"})

    req.refresh_from_db()
    assert req.status == "approved"
    assert response.status_code == 302


# ---------------------------------------------------------------------------
# Method and state guards
# ---------------------------------------------------------------------------


def test_decision_requires_post(client, trainer) -> None:
    """A GET must not decide anything -- link prefetch would otherwise do it."""
    req = an_application_for(trainer)
    client.force_login(trainer)

    assert client.get(decide_url(req)).status_code == 405
    req.refresh_from_db()
    assert req.status == "pending"


def test_already_decided_application_cannot_be_redecided(client, trainer) -> None:
    applicant = make_user("twice@example.test", role=ROLE_TRAINEE, approved=False)
    req = SignupRequest.objects.create(
        email=applicant.email,
        full_name=applicant.full_name,
        user=applicant,
        selected_trainer=trainer,
        status="rejected",
    )
    client.force_login(trainer)

    client.post(decide_url(req), {"decision": "approve"})

    req.refresh_from_db()
    assert req.status == "rejected", "a terminal decision was overwritten"


def test_unknown_uuid_is_404(client, trainer) -> None:
    client.force_login(trainer)
    response = client.post(
        "/accounts/trainer/queue/00000000-0000-0000-0000-000000000000/", {"decision": "approve"}
    )
    assert response.status_code == 404


def test_decision_note_is_length_limited(client, trainer) -> None:
    """An unbounded text field in an audited table is a storage problem."""
    req = an_application_for(trainer)
    client.force_login(trainer)

    client.post(decide_url(req), {"decision": "approve", "note": "x" * 9000})

    req.refresh_from_db()
    assert len(req.decision_note) <= 2000


# ---------------------------------------------------------------------------
# Signup
# ---------------------------------------------------------------------------


def test_signup_creates_an_inert_pending_request(client, trainer) -> None:
    """The core §15 invariant: registering grants nothing."""
    response = client.post(
        "/accounts/signup/",
        {
            "email": "brand.new@example.test",
            "full_name": "Brand New",
            "trainer": str(trainer.pk),
            "password1": GOOD_PASSWORD,
            "password2": GOOD_PASSWORD,
        },
    )
    assert response.status_code == 302

    req = SignupRequest.objects.get(email="brand.new@example.test")
    assert req.status == "pending"
    assert req.user is not None
    assert req.user.is_approved is False
    assert not req.user.is_trainer
    assert sorted(req.user.role_assignments.values_list("role__slug", flat=True)) == [
        ROLE_TRAINEE
    ]


def test_signup_requires_a_trainer(client) -> None:
    response = client.post(
        "/accounts/signup/",
        {
            "email": "notrainer@example.test",
            "full_name": "No Trainer",
            "password1": GOOD_PASSWORD,
            "password2": GOOD_PASSWORD,
        },
    )
    assert response.status_code == 200  # re-rendered with errors
    assert not SignupRequest.objects.filter(email="notrainer@example.test").exists()


def test_signed_in_user_is_bounced_off_the_signup_form(client, approved_trainee) -> None:
    """Only on GET -- see the comment in views.py about discarding a POST."""
    client.force_login(approved_trainee)
    response = client.get("/accounts/signup/")
    assert response.status_code == 302
    assert response["Location"] == "/accounts/pending/"


def test_signed_in_staff_may_still_submit_for_someone_else(client, trainer) -> None:
    """An approver registering on behalf of a trainee must not be redirected.

    Redirecting an authenticated POST would return a 302 that looks like success
    with nothing created.
    """
    client.force_login(trainer)
    response = client.post(
        "/accounts/signup/",
        {
            "email": "on.behalf@example.test",
            "full_name": "On Behalf",
            "trainer": str(trainer.pk),
            "password1": GOOD_PASSWORD,
            "password2": GOOD_PASSWORD,
        },
    )
    assert response.status_code == 302
    assert SignupRequest.objects.filter(email="on.behalf@example.test").exists()


def test_logout_works_without_being_logged_in(client) -> None:
    """Hitting logout twice must not 500."""
    assert client.get("/accounts/logout/").status_code == 302


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


def test_pending_user_may_log_in_and_is_told_why(client, trainee) -> None:
    """Pending accounts are admitted but shown the waiting page (§15)."""
    response = client.post(
        "/accounts/login/", {"username": trainee.email, "password": GOOD_PASSWORD}
    )
    assert response.status_code == 302
    assert response["Location"] == "/accounts/pending/"


def test_login_with_a_wrong_password_does_not_authenticate(client, trainer) -> None:
    response = client.post(
        "/accounts/login/", {"username": trainer.email, "password": "Wr0ng-Passw0rd-99"}
    )
    assert response.status_code == 200
    assert "_auth_user_id" not in client.session


def test_login_does_not_work_for_a_deactivated_account(client, trainer) -> None:
    trainer.is_active = False
    trainer.save(update_fields=["is_active"])
    response = client.post(
        "/accounts/login/", {"username": trainer.email, "password": GOOD_PASSWORD}
    )
    assert "_auth_user_id" not in client.session
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# The queue's shape
#
# A trainer opens this page to make a decision. The default view is therefore the
# pending queue alone; approved and declined are history, reachable but not in
# the way. These pin that, because the failure mode is silent -- the page still
# renders, it just renders the wrong thing.
# ---------------------------------------------------------------------------


def _queue_html(client, trainer, query: str = "") -> str:
    client.force_login(trainer)
    return client.get(f"{QUEUE_URL}{query}").content.decode()


def test_the_queue_opens_on_pending_not_on_history(client, trainer) -> None:
    applicant = make_user("fresh@example.test", approved=False)
    decided = SignupRequest.objects.create(
        email="old@example.test",
        full_name="Already Decided",
        selected_trainer=trainer,
        status="approved",
    )
    waiting = SignupRequest.objects.create(
        email=applicant.email,
        full_name="Waiting For Me",
        selected_trainer=trainer,
        user=applicant,
    )
    assert decided.status == "approved"

    html = _queue_html(client, trainer)

    assert "Waiting For Me" in html, "the pending applicant is missing from the default view"
    assert "Already Decided" not in html, "history is crowding out the pending queue"


def test_each_status_filter_shows_only_its_own_rows(client, trainer) -> None:
    applicant = make_user("mine@example.test", approved=False)
    SignupRequest.objects.create(
        email=applicant.email,
        full_name="Pending One",
        selected_trainer=trainer,
        user=applicant,
    )
    SignupRequest.objects.create(
        email="ok@example.test",
        full_name="Approved One",
        selected_trainer=trainer,
        status="approved",
    )
    SignupRequest.objects.create(
        email="no@example.test",
        full_name="Declined One",
        selected_trainer=trainer,
        status="rejected",
    )

    approved = _queue_html(client, trainer, "?filter=approved")
    assert "Approved One" in approved
    assert "Pending One" not in approved, "the approved tab is leaking pending rows"

    declined = _queue_html(client, trainer, "?filter=rejected")
    assert "Declined One" in declined
    assert "Approved One" not in declined, "the declined tab is leaking approved rows"


def test_the_tabs_report_counts_that_match_the_list(client, trainer) -> None:
    """A count that disagrees with its own list is worse than no count."""
    for n in range(3):
        applicant = make_user(f"wait{n}@example.test", approved=False)
        SignupRequest.objects.create(
            email=applicant.email,
            full_name=f"Waiting {n}",
            selected_trainer=trainer,
            user=applicant,
        )
    SignupRequest.objects.create(
        email="done@example.test",
        full_name="Done One",
        selected_trainer=trainer,
        status="approved",
    )

    html = _queue_html(client, trainer)

    assert "Waiting 0" in html and "Waiting 2" in html
    pending_tab = re.search(r'href="\?filter=pending".*?</a>', html, flags=re.S).group(0)
    assert "3" in pending_tab, f"the pending tab miscounts: {pending_tab!r}"


def test_an_unknown_filter_falls_back_instead_of_erroring(client, trainer) -> None:
    """A stale bookmark should not be a 500."""
    client.force_login(trainer)

    response = client.get(f"{QUEUE_URL}?filter=not-a-status")

    assert response.status_code == 200
    assert "Nothing waiting on you" in response.content.decode(), (
        "an unknown filter should fall back to the pending view"
    )


def test_only_pending_rows_offer_the_decision_buttons(client, trainer) -> None:
    """Approving is not idempotent; a decided row must not offer the button."""
    SignupRequest.objects.create(
        email="done@example.test",
        full_name="Done One",
        selected_trainer=trainer,
        status="approved",
    )

    html = _queue_html(client, trainer, "?filter=approved")

    assert "Done One" in html
    assert 'name="decision"' not in html, "a decided row still offers Approve/Decline"


def test_the_empty_pending_queue_says_so_rather_than_being_blank(client, trainer) -> None:
    html = _queue_html(client, trainer)

    assert "Nothing waiting on you" in html


# ---------------------------------------------------------------------------
# Approve and Decline must stay distinguishable
#
# Reported as "both buttons work for decline". The server was always correct --
# both decisions arrived and were stored correctly -- but the confirm prompt sat
# on the <form>, so it had to be worded for the riskier action. Clicking Approve
# therefore opened a dialog that read "Decline ...?". A trainer who read it
# reasonably concluded they had declined.
#
# The tests below pin the prompt to the button, which is the actual fix.
# ---------------------------------------------------------------------------


def _queue_markup(client, trainer) -> str:
    applicant = make_user("prompts@example.test", approved=False)
    SignupRequest.objects.create(
        email=applicant.email,
        full_name="Prompt Target",
        selected_trainer=trainer,
        user=applicant,
    )
    client.force_login(trainer)
    return client.get(QUEUE_URL).content.decode()


def test_each_decision_button_carries_its_own_prompt(client, trainer) -> None:
    html = _queue_markup(client, trainer)

    # Bound each slice to the end of its own opening tag. A fixed-width slice
    # runs past Approve into Decline and reports a false failure.
    def button(value: str) -> str:
        start = html.index(f'value="{value}"')
        return html[start : html.index(">", start)]

    approve = button("approve")
    reject = button("reject")
    redirect = button("redirect")

    assert 'data-confirm="Approve Prompt Target?"' in approve
    assert 'data-confirm-label="Approve"' in approve
    assert 'data-confirm-tone="approve"' in approve
    assert "Decline" not in approve, "the Approve button still talks about declining"

    # "permanently" is load-bearing: it is the only thing on screen telling the
    # trainer this choice blocks the address, and there is now a third button
    # beside it that declines without blocking.
    assert 'data-confirm="Decline Prompt Target permanently?"' in reject
    assert 'data-confirm-label="Decline permanently"' in reject
    assert 'data-confirm-tone="reject"' in reject
    assert "Approve?" not in reject

    # The soft decline must not describe itself as permanent, or the two
    # decline buttons become indistinguishable at the moment of choosing.
    assert 'data-confirm="Decline Prompt Target but allow them to re-register?"' in redirect
    assert 'data-confirm-label="Allow re-register"' in redirect
    assert "permanently" not in redirect
    assert "Approve?" not in redirect



def test_the_prompt_is_not_shared_by_the_form(client, trainer) -> None:
    """A form-level prompt is the bug itself: one wording cannot fit two buttons."""
    html = _queue_markup(client, trainer)
    form_tag = html[html.rindex("<form", 0, html.index('value="approve"')) :]
    form_tag = form_tag[: form_tag.index(">") + 1]

    assert "data-confirm" not in form_tag


def test_the_confirmation_dialog_is_ours_not_the_browsers(client, trainer) -> None:
    """Replaced window.confirm, which ignores the design system entirely."""
    login = client.get("/accounts/login/").content.decode()

    assert '<dialog class="confirm" id="confirm-dialog"' in login
    assert 'aria-labelledby="confirm-title"' in login
    assert 'aria-describedby="confirm-body"' in login
    assert "data-confirm-accept" in login
    assert "data-confirm-cancel" in login

    js = Path("src/frontend/static/js/portal.js").read_text()
    # Match a call, not a mention: the explanatory comment above the handler
    # names window.confirm, and that comment is the whole point of the section.
    assert not re.search(r"window\.confirm\s*\(", js), (
        "the browser dialog is still wired up"
    )
    assert "requestSubmit(button)" in js, (
        "form.submit() would drop the button's value and decline everything"
    )

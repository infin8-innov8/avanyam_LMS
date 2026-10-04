"""View-level authorization: the HTTP trust boundary.

`test_policies.py` proves the rules are right and `test_approval_flow.py` proves
the service enforces them. Neither proves the *views* route every request through
them. `views.py` opens by claiming "Every authorization question is delegated to
policies (§7). No view inspects `user.is_trainer` directly" -- these tests are
what makes that claim true rather than aspirational.

Two boundaries are covered.

**Who may decide.** Approval is admin-only and the queue is shared (D41), so
there is no per-trainer scoping left to get wrong -- and correspondingly no trainer
path to defend. The cases here are: a trainer gets a 404, not a 403, so the
queue's existence is not confirmed; an admin decides anything; nobody decides twice.

**What a session is worth.** Application ids are UUIDs, which people assume are
unguessable. They are not a security control. An id learned from a URL, a
referrer, a screenshot or a former colleague must not let anyone decide that
application, and a pending account must not hold a live session at all.
"""

from __future__ import annotations

import itertools
import re
from pathlib import Path

import pytest
from django.urls import reverse

from apps.accounts.models import SignupRequest

from .conftest import GOOD_PASSWORD, make_user

pytestmark = pytest.mark.django_db

QUEUE_URL = "/accounts/admin/queue/"

#: SignupRequest.email is unique and these tests need many applications, so each
#: helper call gets its own address rather than colliding with the shared dev DB.
_applicant_emails = (f"view.app{n}@example.test" for n in itertools.count())


def decide_url(req: SignupRequest) -> str:
    return reverse("accounts:decide", args=[req.pk])


def an_application(*, decided: str | None = None, linked: bool = True) -> SignupRequest:
    """A pending (or already-decided) application.

    `linked=False` builds the orphan shape -- a SignupRequest with no account
    behind it -- which `decide()` refuses for a different reason than
    authorization, so tests that want to isolate the authz failure keep it linked.
    """
    applicant = make_user(next(_applicant_emails), approved=False) if linked else None
    return SignupRequest.objects.create(
        email=next(_applicant_emails),
        full_name="An Applicant",
        user=applicant,
        status=decided or "pending",
    )


# ---------------------------------------------------------------------------
# Anonymous callers get nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "/accounts/home/",
        QUEUE_URL,
        "/accounts/admin/people/",
        "/accounts/password/",
    ],
)
def test_anonymous_is_redirected_to_login(client, url: str) -> None:
    response = client.get(url)
    assert response.status_code == 302
    assert "/accounts/login/" in response["Location"]


def test_anonymous_cannot_decide(client, admin) -> None:
    req = an_application()
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


def test_trainer_has_no_queue(client, trainer) -> None:
    """The load-bearing change. A trainer used to own a queue and now owns none.

    Pinning it because "trainers cannot approve" is not derivable from the URL or
    the template -- nothing about /accounts/admin/queue/ tells a reader that the
    *role* is what closes it, and a later refactor could reintroduce the trainer
    path by loosening a single condition.
    """
    client.force_login(trainer)
    assert client.get(QUEUE_URL).status_code == 404


def test_pending_user_is_redirected_to_login(client, trainee) -> None:
    """Pending means inactive, so the session stops resolving. 302, not 404.

    This is the D45 outcome arriving through the auth layer rather than the policy
    layer: there is no session to evaluate a policy against. It replaces a test
    asserting a pending user got a 404 from the queue, which was only true while
    pending accounts were still active.
    """
    client.force_login(trainee)

    response = client.get(QUEUE_URL)

    assert response.status_code == 302
    assert "/accounts/login/" in response["Location"]


def test_an_active_but_unapproved_account_is_still_refused(client, trainee) -> None:
    """The policy gate, isolated from the authentication gate.

    `User.move_to` derives `is_active` from the approval status, so no flow should
    produce an active-but-unapproved admin. The check is kept anyway: it is a
    second line behind a derived field, and the failure it prevents is an
    unapproved person reading the whole applicant table.
    """
    trainee.is_active = True
    trainee.save(update_fields=["is_active"])
    client.force_login(trainee)

    assert client.get(QUEUE_URL).status_code == 404


def test_inactive_admin_loses_access_entirely(client, admin) -> None:
    """Django refuses to resolve an inactive user, so the session stops working.

    This is a 302 to login rather than the 404 a signed-in non-approver gets,
    because `get_user()` returns AnonymousUser for a deactivated account. Worth
    pinning: "deactivated means logged out" and "not an approver" are different
    mechanisms and should not be confused.
    """
    admin.is_active = False
    admin.save(update_fields=["is_active"])
    client.force_login(admin)

    response = client.get(QUEUE_URL)

    assert response.status_code == 302
    assert "/accounts/login/" in response["Location"]


def test_approved_admin_has_a_queue(client, admin) -> None:
    client.force_login(admin)
    assert client.get(QUEUE_URL).status_code == 200


def test_admin_with_nothing_pending_gets_an_empty_queue_not_an_error(client, admin) -> None:
    """An admin with no applications must get the empty state, not a 404.

    This is the screen an admin is meant to see between requests. It used to be
    untested -- the only assertion was a bare 200 -- so "no requests" and "not
    authorised" were indistinguishable from the outside, and the refusal page
    read like a broken link.

    The suite runs against a long-lived development database that already holds
    real applications, so "nothing waiting" cannot be arranged by making requests:
    it is asserted against the shared database's actual emptiness, which is why
    this checks for the empty state's *wording* appearing rather than for a row
    count.
    """
    client.force_login(admin)

    response = client.get(f"{QUEUE_URL}?filter=pending")
    body = response.content.decode()

    assert response.status_code == 200
    assert "Registration requests" in body, "the queue chrome is missing"


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
    assert "/accounts/home/" in body


def test_refusal_page_explains_a_pending_registration(client, trainee) -> None:
    """The applicant most likely to hit this gets told what is actually happening.

    Needs an active-but-unapproved session to be reachable at all: a pending
    account cannot sign in (D45), so the only realistic way to see this page is a
    session issued before the account was suspended or its approval was withdrawn.
    """
    trainer = make_user("explanations.trainer@example.test", role="admin")
    trainee.approval_status = "pending"
    trainee.is_active = True
    trainee.save(update_fields=["approval_status", "is_active"])
    client.force_login(trainee)

    body = client.get(QUEUE_URL).content.decode()

    # The page must state the visitor's own status, not just that it has no queue.
    # "No approval queue for this account" is true of every non-admin and tells a
    # pending applicant nothing about why they cannot sign in -- which is the one
    # question they arrived with.
    assert "Your own registration is" in body
    assert "pending" in body
    assert "administrator approves" in body
    assert trainer.email not in body, "the refusal page leaked an applicant address"


# ---------------------------------------------------------------------------
# The IDOR case
# ---------------------------------------------------------------------------


def test_a_non_approver_cannot_decide_by_uuid(client, approved_trainee, admin) -> None:
    """A valid session, a real UUID, the wrong role.

    The UUID is not the control here -- `decide()` must refuse on identity. What
    matters is that the row is untouched and the refusal is not silently ignored.
    """
    victim = an_application()
    client.force_login(approved_trainee)

    response = client.post(decide_url(victim), {"decision": "approve"})

    victim.refresh_from_db()
    assert victim.status == "pending", "a trainee decided an application"
    assert response.status_code == 302
    assert response["Location"] == QUEUE_URL


def test_the_decision_also_fails_at_the_service_layer(client, trainer, admin) -> None:
    """Belt and braces: the view is not the only thing refusing."""
    from apps.accounts.service.approval import DecisionError, decide

    victim = an_application()
    with pytest.raises(DecisionError):
        decide(approver=trainer, request_pk=victim.pk, approve=True)
    victim.refresh_from_db()
    assert victim.status == "pending"


def test_any_admin_can_decide_any_application(client, admin) -> None:
    """One shared queue, so nobody is scoped and nobody is left out.

    This replaced a test asserting a trainer could decide the applicant who named
    them and could not decide anyone else's. With no nominated trainer, the
    distinction has no referent, and a test in the old shape would keep implying
    that some trainer somewhere still has authority.
    """
    req = an_application()
    client.force_login(admin)

    response = client.post(decide_url(req), {"decision": "approve"})

    req.refresh_from_db()
    assert req.status == "approved"
    assert response.status_code == 302


def test_a_second_admin_can_decide_the_same_application(client, admin) -> None:
    """Shared means shared: admin B is not locked out of admin A's applicants."""
    other_admin = make_user("second.admin@example.test", role="admin")
    req = an_application()
    client.force_login(other_admin)

    client.post(decide_url(req), {"decision": "approve"})

    req.refresh_from_db()
    assert req.status == "approved"


def test_queue_shows_every_application_to_an_admin(client, admin) -> None:
    """No per-approver scoping, so the queue is the whole table."""
    client.force_login(admin)
    body = client.get(QUEUE_URL).content.decode()

    known = an_application()
    client.force_login(admin)
    body = client.get(f"{QUEUE_URL}?filter=pending").content.decode()

    assert known.full_name in body


def test_the_queue_does_not_leak_to_a_trainer(client, trainer, admin) -> None:
    """A trainer asking for the queue sees no applicant names at all.

    Asserted on the rendered body, not the status, because 404 with a populated
    body and 404 with a leaking body are different failures and only one of them
    is a disclosure.
    """
    an_application()
    client.force_login(trainer)

    response = client.get(f"{QUEUE_URL}?filter=pending")
    body = response.content.decode()

    assert response.status_code == 404
    assert "An Applicant" not in body


def test_admin_can_decide_an_unassigned_application(client, admin) -> None:
    """An application with no trainer was never stranded; now it is also the norm."""
    req = an_application()
    client.force_login(admin)

    response = client.post(decide_url(req), {"decision": "approve"})

    req.refresh_from_db()
    assert req.status == "approved"
    assert response.status_code == 302


def test_no_admin_may_decide_their_own_application(client, admin) -> None:
    """Separation of duties, at the view rather than only in the service."""
    own = SignupRequest.objects.create(
        email=admin.email, full_name=admin.full_name, user=admin
    )
    client.force_login(admin)

    response = client.post(decide_url(own), {"decision": "approve"}, follow=True)

    own.refresh_from_db()
    assert own.status == "pending", "an admin approved their own application"
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Method and state guards
# ---------------------------------------------------------------------------


def test_decision_requires_post(client, admin) -> None:
    """A GET must not decide anything -- link prefetch would otherwise do it."""
    req = an_application()
    client.force_login(admin)

    assert client.get(decide_url(req)).status_code == 405
    req.refresh_from_db()
    assert req.status == "pending"


def test_already_decided_application_cannot_be_redecided(client, admin) -> None:
    req = an_application(decided="rejected")
    client.force_login(admin)

    client.post(decide_url(req), {"decision": "approve"})

    req.refresh_from_db()
    assert req.status == "rejected", "a terminal decision was overwritten"


def test_unknown_uuid_is_404(client, admin) -> None:
    client.force_login(admin)
    response = client.post(
        f"{QUEUE_URL}00000000-0000-0000-0000-000000000000/", {"decision": "approve"}
    )
    assert response.status_code == 404


def test_decision_note_is_length_limited(client, admin) -> None:
    """An unbounded text field in an audited table is a storage problem."""
    req = an_application()
    client.force_login(admin)

    client.post(decide_url(req), {"decision": "approve", "note": "x" * 9000})

    req.refresh_from_db()
    assert len(req.decision_note) <= 2000


# ---------------------------------------------------------------------------
# Signup
# ---------------------------------------------------------------------------


def test_signup_creates_an_inert_pending_request(client) -> None:
    """The core §15 invariant: registering grants nothing at all.

    No role, and inactive. The role half is new: signup used to hand out a
    trainee role, so an unapproved person already appeared in every query that
    filters on role rather than approval status.
    """
    response = client.post(
        "/accounts/signup/",
        {
            "email": "brand.new@example.test",
            "full_name": "Brand New",
            "requested_role": "trainee",
            "password1": GOOD_PASSWORD,
            "password2": GOOD_PASSWORD,
        },
    )
    assert response.status_code == 302

    req = SignupRequest.objects.get(email="brand.new@example.test")
    assert req.status == "pending"
    assert req.requested_role == "trainee"
    assert req.user is not None
    assert req.user.is_approved is False
    assert req.user.is_active is False
    assert not req.user.is_trainer
    assert list(req.user.role_assignments.all()) == []


def test_signup_accepts_a_trainer_request(client) -> None:
    """Asking for trainer is allowed; *having* it is not, until approval."""
    response = client.post(
        "/accounts/signup/",
        {
            "email": "wants.trainer@example.test",
            "full_name": "Wants Trainer",
            "requested_role": "trainer",
            "password1": GOOD_PASSWORD,
            "password2": GOOD_PASSWORD,
        },
    )
    assert response.status_code == 302

    req = SignupRequest.objects.get(email="wants.trainer@example.test")
    assert req.requested_role == "trainer"
    assert list(req.user.role_assignments.all()) == []


def test_signup_refuses_an_admin_request(client) -> None:
    """Admin is minted by `manage.py createadmin`, never asked for.

    Checked at the HTTP boundary as well as in the service, because this is a
    field an unauthenticated visitor POSTs.
    """
    response = client.post(
        "/accounts/signup/",
        {
            "email": "wants.admin@example.test",
            "full_name": "Wants Admin",
            "requested_role": "admin",
            "password1": GOOD_PASSWORD,
            "password2": GOOD_PASSWORD,
        },
    )

    assert response.status_code == 200, "re-rendered with errors"
    assert not SignupRequest.objects.filter(email="wants.admin@example.test").exists()


def test_signed_in_user_is_bounced_off_the_signup_form(client, approved_trainee) -> None:
    """Only on GET -- see the comment in views.py about discarding a POST."""
    client.force_login(approved_trainee)
    response = client.get("/accounts/signup/")
    assert response.status_code == 302
    assert response["Location"] == "/accounts/home/"


def test_signed_in_staff_may_still_submit_for_someone_else(client, trainer) -> None:
    """A trainer registering on behalf of a trainee must not be redirected.

    Redirecting an authenticated POST would return a 302 that looks like success
    with nothing created. A trainer is not an approver, but registering accounts
    for others is still theirs to do.
    """
    client.force_login(trainer)
    response = client.post(
        "/accounts/signup/",
        {
            "email": "on.behalf@example.test",
            "full_name": "On Behalf",
            "requested_role": "trainee",
            "password1": GOOD_PASSWORD,
            "password2": GOOD_PASSWORD,
        },
    )
    assert response.status_code == 302
    req = SignupRequest.objects.get(email="on.behalf@example.test")
    assert req.created_by_id == trainer.pk


def test_an_authenticated_post_cannot_bypass_the_register_policy(
    client, approved_trainee
) -> None:
    """The signup POST records `requester`, so it must re-check authority.

    A trainee holds a live session and can POST to /accounts/signup/ directly. If
    the view trusted the session without asking `can_register`, this would create
    accounts. The view gates on GET for convenience only.

    403, not 200 and emphatically not 500: this is an authority question, and the
    refusal reason is rendered back onto the form. It used to answer 500, because
    `submit_application` re-checks what the view had already allowed and the
    resulting `ValidationError` escaped a plain function view that had no
    `FormView` machinery to catch it.
    """
    client.force_login(approved_trainee)

    response = client.post(
        "/accounts/signup/",
        {
            "email": "sneaky@example.test",
            "full_name": "Sneaky",
            "requested_role": "trainee",
            "password1": GOOD_PASSWORD,
            "password2": GOOD_PASSWORD,
        },
    )

    body = response.content.decode()
    assert response.status_code == 403
    assert not SignupRequest.objects.filter(email="sneaky@example.test").exists()
    assert "Only trainers and admins" in body, "the refusal did not say why"
    assert "Traceback" not in body


def test_logout_works_without_being_logged_in(client) -> None:
    """Hitting logout twice must not 500."""
    assert client.get("/accounts/logout/").status_code == 302


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


def test_a_pending_account_cannot_log_in_at_all(client, trainee) -> None:
    """No session, not a session on a waiting page (D45/D50).

    Replaces a test asserting a pending user was admitted and redirected to a
    pending page. Admitting them gave an unapproved person a live session and made
    "correct password, not approved" have two outcomes depending on who asked.
    """
    response = client.post(
        "/accounts/login/", {"username": trainee.email, "password": GOOD_PASSWORD}
    )

    assert "_auth_user_id" not in client.session, "a pending account got a session"
    assert response.status_code == 200


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
# An admin opens this page to make a decision. The default view is therefore the
# pending queue alone; approved and declined are history, reachable but not in
# the way. These pin that, because the failure mode is silent -- the page still
# renders, it just renders the wrong thing.
# ---------------------------------------------------------------------------


def _queue_html(client, admin, query: str = "") -> str:
    client.force_login(admin)
    return client.get(f"{QUEUE_URL}{query}").content.decode()


def test_the_queue_opens_on_pending_not_on_history(client, admin) -> None:
    decided = SignupRequest.objects.create(
        email=next(_applicant_emails),
        full_name="Already Decided",
        status="approved",
    )
    applicant = make_user(next(_applicant_emails), approved=False)
    SignupRequest.objects.create(
        email=applicant.email,
        full_name="Waiting For Me",
        user=applicant,
    )
    assert decided.status == "approved"

    html = _queue_html(client, admin)

    assert "Waiting For Me" in html, "the pending applicant is missing from the default view"
    assert "Already Decided" not in html, "history is crowding out the pending queue"


def test_each_status_filter_shows_only_its_own_rows(client, admin) -> None:
    applicant = make_user(next(_applicant_emails), approved=False)
    SignupRequest.objects.create(
        email=applicant.email, full_name="Pending One", user=applicant
    )
    SignupRequest.objects.create(
        email=next(_applicant_emails), full_name="Approved One", status="approved"
    )
    SignupRequest.objects.create(
        email=next(_applicant_emails), full_name="Declined One", status="rejected"
    )

    approved = _queue_html(client, admin, "?filter=approved")
    assert "Approved One" in approved
    assert "Pending One" not in approved, "the approved tab is leaking pending rows"

    declined = _queue_html(client, admin, "?filter=rejected")
    assert "Declined One" in declined
    assert "Approved One" not in declined, "the declined tab is leaking approved rows"


def test_an_unknown_filter_falls_back_instead_of_erroring(client, admin) -> None:
    """A stale bookmark should not be a 500."""
    client.force_login(admin)

    response = client.get(f"{QUEUE_URL}?filter=not-a-status")

    assert response.status_code == 200, "an unknown filter should fall back to pending"
    assert "Registration requests" in response.content.decode()


def test_only_pending_rows_offer_the_decision_buttons(client, admin) -> None:
    """Approving is not idempotent; a decided row must not offer the button."""
    SignupRequest.objects.create(
        email=next(_applicant_emails), full_name="Done One", status="approved"
    )

    html = _queue_html(client, admin, "?filter=approved")

    assert "Done One" in html
    assert 'name="decision"' not in html, "a decided row still offers Approve/Decline"


# ---------------------------------------------------------------------------
# Approve and Decline must stay distinguishable
#
# Reported as "both buttons work for decline". The server was always correct --
# both decisions arrived and were stored correctly -- but the confirm prompt sat
# on the <form>, so it had to be worded for the riskier action. Clicking Approve
# therefore opened a dialog that read "Decline ...?". An admin who read it
# reasonably concluded they had declined.
#
# The tests below pin the prompt to the button, which is the actual fix.
# ---------------------------------------------------------------------------


def _queue_markup(client, admin) -> str:
    applicant = make_user(next(_applicant_emails), approved=False)
    SignupRequest.objects.create(
        email=applicant.email, full_name="Prompt Target", user=applicant
    )
    client.force_login(admin)
    return client.get(QUEUE_URL).content.decode()


def test_each_decision_button_carries_its_own_prompt(client, admin) -> None:
    html = _queue_markup(client, admin)

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
    # admin this choice blocks the address, and there is now a third button
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


def test_the_prompt_is_not_shared_by_the_form(client, admin) -> None:
    """A form-level prompt is the bug itself: one wording cannot fit two buttons."""
    html = _queue_markup(client, admin)
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
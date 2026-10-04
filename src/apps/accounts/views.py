"""Views for registration, login, and the trainer approval queue.

Every authorization question is delegated to `policies` (§7). No view inspects
`user.is_trainer` directly.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, PasswordChangeView
from django.db import transaction
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.forms import LoginForm, SignupForm
from apps.accounts.models import (
    APPROVAL_UNDO_LIFETIME_MINUTES,
    ApprovalUndoToken,
    SignupRequest,
)
from apps.accounts.policies import (
    can_undo,
    can_view_queue,
    can_view_request,
    is_approved,
    visible_requests,
)

from apps.accounts.service.approval import Decision, DecisionError, decide
from apps.accounts.service.signup import signup_enabled
from apps.accounts.service.undo import UndoError, request_undo_code, undo_rejection


class PortalLoginView(LoginView):
    """Branded login. Pending accounts are admitted but shown the waiting page."""

    template_name = "accounts/login.html"
    authentication_form = LoginForm
    redirect_authenticated_user = False

    def form_valid(self, form):
        response = super().form_valid(form)
        if not is_approved(self.request.user):
            messages.info(
                self.request,
                "Your account is awaiting trainer approval. You can sign in, but "
                "training content stays locked until you are approved.",
            )
        return response

    def get_success_url(self) -> str:
        return self.get_redirect_url() or reverse_lazy("accounts:pending")


def signup(request):
    # Only bounce an already-signed-in visitor on GET. Redirecting an
    # authenticated POST would silently discard the submission and return a 302
    # that looks like success -- an approved staff member submitting on behalf of
    # a trainee would see "request received" with nothing created.
    if request.user.is_authenticated and request.method == "GET":
        return redirect("accounts:pending")
    if not signup_enabled():
        return render(
            request,
            "accounts/signup_closed.html",
            {"support_email": "avanyam.official@gmail.com"},
            status=503,
        )

    form = SignupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        result = form.save(requester=request.user if request.user.is_authenticated else None)
        return redirect("accounts:applied")

    return render(
        request,
        "accounts/signup.html",
        {"form": form, "signup_enabled": True},
    )


def applied(request):
    return render(request, "accounts/applied.html")


#: The status tabs offered on the approval queue, in display order. `all` is
#: reachable by hand but not offered, because mixing pending with decided
#: history is the thing this page was redesigned to stop doing.
#:
#: `redirected` sits next to `rejected` because a trainer needs to be able to see
#: that they sent someone away "try another trainer" -- without it the row is
#: only reachable under `all`, and in practice nobody checks `all`.
QUEUE_FILTERS = ("pending", "approved", "rejected", "redirected")

#: Tab labels. A status name is not a sentence a trainer should have to decode,
#: and "redirected" in particular means nothing to someone who did not choose it.
QUEUE_FILTER_LABELS = {
    "pending": "Pending",
    "approved": "Approved",
    "rejected": "Declined",
    "redirected": "Told to try again",
}


@login_required
def pending(request):
    """Where every freshly-registered user lands."""
    return render(
        request,
        "accounts/pending.html",
        {
            "user_obj": request.user,
            "is_approved": request.user.is_approved,
            "trainer": request.user.selected_trainer,
        },
    )


@login_required
def trainer_queue(request):
    """Applications this trainer must decide.

    Access is a policy call, and the list is filtered by the same policy, so a
    trainer can never see -- let alone act on -- someone else's applicant by
    guessing a UUID in the URL.

    A signed-in non-approver still gets 404 rather than 403, because a 403 would
    confirm the queue exists. What changed is only how that 404 looks: Django's
    handler renders its own technical page whenever DEBUG is on and ignores
    404.html entirely, so the explanation is rendered here instead. Raising
    Http404 and waiting for a handler produced a stack trace with "Page not
    found" as the entire answer, which reads like a broken link rather than a
    deliberate refusal -- which is exactly how it was misread during testing.
    """
    if not can_view_queue(request.user):
        return render(
            request,
            "accounts/no_queue.html",
            {"pending_state": request.user.approval_status},
            status=404,
        )

    rows = visible_requests(request.user)

    # A single list mixing "waiting for you" with everything already decided is
    # the wrong default: the one thing a trainer opened this page to do is buried
    # under history. Pending is therefore the default view, and the counts are
    # computed over the same rows so the tab labels cannot disagree with the
    # list they switch.
    counts = {status: 0 for status in QUEUE_FILTERS}
    for row in rows:
        if row.status in counts:
            counts[row.status] += 1

    selected = request.GET.get("filter") or "pending"
    if selected not in (*QUEUE_FILTERS, "all"):
        # An unknown filter is a stale bookmark, not an attack; fall back rather
        # than 400.
        selected = "pending"

    if selected == "all":
        shown = rows
    else:
        shown = [row for row in rows if row.status == selected]

    # Built here rather than looked up in the template: Django cannot do
    # `counts[status]` with a variable key, and the obvious workaround -- a
    # custom `get_item` filter -- would add a template tag to express one dict
    # read. Passing finished rows keeps the arithmetic in Python, where it is
    # testable, and lets a tab be marked current without a second comparison in
    # the markup.
    tabs = [
        {
            "status": status,
            "label": QUEUE_FILTER_LABELS[status],
            "count": counts[status],
            "is_current": status == selected,
        }
        for status in QUEUE_FILTERS
        if counts[status] or status == "pending"
    ]

    # Per-row capability, decided once here rather than in the markup. The
    # template has no way to ask "may this viewer undo this row", and a button
    # that renders for everyone and 403s on submit teaches trainers that the
    # queue lies.
    #
    # Set as an attribute on the row rather than collected into a dict keyed by
    # pk: Django cannot do `dict[variable]` in a template, and the workaround for
    # that (a custom `get_item` filter) is a new template tag to express one
    # lookup. The attribute rides along on the instance the template already has.
    for row in shown:
        row.undo_allowed = (
            can_view_request(request.user, row).allowed and can_undo(row).allowed
        )

    # Rows where this viewer already has a live code waiting. The dialog uses this
    # to open straight on the code box rather than mailing a second code, which
    # would expire the one they are reading.
    undoable = [row.pk for row in shown if row.undo_allowed]
    with_code = (
        set(_live_codes(request.user, undoable).values_list("request_id", flat=True))
        if undoable
        else set()
    )
    for row in shown:
        row.code_sent = row.pk in with_code

    return render(
        request,
        "accounts/trainer_queue.html",
        {
            "requests": shown,
            "tabs": tabs,
            "counts": counts,
            "total": len(rows),
            "selected": selected,
        },
    )


#: A live code is one this viewer requested, that has not been used, and that has
#: not expired. Built as a filter rather than a predicate so the queue can ask
#: about every row in one query instead of one per row.
def _live_codes(trainer, request_ids):
    return ApprovalUndoToken.objects.filter(
        request_id__in=request_ids,
        requested_by=trainer,
        consumed_at__isnull=True,
        expires_at__gt=timezone.now(),
    )


@login_required
@require_POST
def decide_request(request, request_pk):
    # Passed through as-is rather than collapsed to `approve = ... == "approve"`.
    # That comparison made every non-approve value a rejection, so the redirect
    # option could not have worked even once the service supported it.
    posted = request.POST.get("decision", "")
    try:
        outcome = decide(
            approver=request.user,
            request_pk=request_pk,
            decision=posted,
            note=request.POST.get("note", "")[:2000],
        )
    except SignupRequest.DoesNotExist as exc:
        raise Http404("No such application.") from exc
    except DecisionError as exc:
        messages.error(request, str(exc))
        return redirect("accounts:trainer-queue")

    messages.success(
        request,
        f"{outcome.request.full_name} was {_DECISION_PAST_TENSE[posted]}.",
    )
    return redirect("accounts:trainer-queue")


#: What to tell the trainer they just did. Keyed by the posted value, so an
#: unrecognised decision cannot silently produce the wrong sentence -- the service
#: has already refused it by the time this is looked up.
_DECISION_PAST_TENSE = {
    Decision.APPROVE.value: "approved",
    Decision.REJECT.value: "declined permanently",
    Decision.REDIRECT.value: "declined, and told they may register again",
}


#: A refusal is reported to the browser as JSON when the dialog asked for it, and
#: as a flash message plus a redirect when a plain form post arrived. Both shapes
#: come from the same service error, so the dialog and the no-JS fallback can
#: never disagree about *why* something failed.
def _wants_json(request) -> bool:
    return (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in request.headers.get("Accept", "")
    )


def _undo_refused(request, reason: str):
    if _wants_json(request):
        return JsonResponse({"ok": False, "error": reason}, status=400)
    messages.error(request, reason)
    return redirect("accounts:trainer-queue")


@login_required
def undo_request_page(request, request_pk):
    """The undo flow as a page of its own, for trainers without JS.

    The queue row carries one button and nothing else. With JS the dialog does
    both steps in place; without it, posting the button lands here, which is the
    only other place the code box appears. Keeping it off the queue is the point:
    a second control beside "Undo decline" turned one action into a choice between
    two, and let a trainer submit an empty code before any existed.
    """
    req = get_object_or_404(
        SignupRequest.objects.select_related("user", "selected_trainer"),
        pk=request_pk,
    )
    visible = can_view_request(request.user, req).allowed
    if not (visible and can_undo(req).allowed):
        raise Http404("No such application.")

    return render(
        request,
        "accounts/undo_request.html",
        {
            "req": req,
            "code_sent": _live_codes(request.user, [req.pk]).exists(),
            "minutes": APPROVAL_UNDO_LIFETIME_MINUTES,
        },
    )


@login_required
@require_POST
def request_undo_code_view(request, request_pk):
    """Mail the trainer a code so they can undo a decline."""
    try:
        issued = request_undo_code(trainer=request.user, request_pk=request_pk)
    except SignupRequest.DoesNotExist as exc:
        raise Http404("No such application.") from exc
    except UndoError as exc:
        return _undo_refused(request, str(exc))

    minutes = APPROVAL_UNDO_LIFETIME_MINUTES
    if _wants_json(request):
        # `email` is the trainer's own address, returned so the dialog can say
        # where the code went. It is the address the request was authenticated as,
        # so this discloses nothing the page does not already know.
        return JsonResponse(
            {
                "ok": True,
                "email": issued.token.requested_by.email,
                "minutes": minutes,
            }
        )

    messages.success(
        request,
        f"A confirmation code is on its way to {issued.token.requested_by.email}. "
        f"It is valid for {minutes} minutes.",
    )
    return redirect("accounts:undo-request", request_pk=request_pk)


@login_required
@require_POST
def undo_rejection_view(request, request_pk):
    """Redeem the code and put the application back in the queue."""
    try:
        outcome = undo_rejection(
            trainer=request.user,
            request_pk=request_pk,
            code=request.POST.get("code", ""),
        )
    except SignupRequest.DoesNotExist as exc:
        raise Http404("No such application.") from exc
    except UndoError as exc:
        # A refused code leaves the application rejected -- the service only moves
        # the row inside `_redeem`, and nothing was redeemed. The dialog stays on
        # its second step so the trainer can try again or ask for a new code.
        return _undo_refused(request, str(exc))

    if _wants_json(request):
        return JsonResponse(
            {"ok": True, "full_name": outcome.request.full_name, "status": "pending"}
        )

    messages.success(
        request,
        f"{outcome.request.full_name} is back in the approval queue. "
        "They have been told their decline was undone.",
    )
    return redirect("accounts:trainer-queue")


def logout_view(request):
    auth_logout(request)
    return redirect("accounts:login")


class FirstPasswordChangeView(PasswordChangeView):
    """Clears `must_change_password` on success, and says so by email."""

    template_name = "accounts/password_change.html"
    success_url = reverse_lazy("accounts:pending")

    def form_valid(self, form):
        # Imported here, not at module scope, matching signup.py and approval.py:
        # tasks imports models, and views is imported by tasks' own module graph
        # via the URLconf.
        from apps.accounts.tasks import notify_user_of_password_change

        # Read this before super(), which logs the user out. Keeping the flag
        # lets the notification distinguish the seeded bootstrap change from an
        # ordinary one, which are very different events to receive an email about.
        was_first_run = self.request.user.must_change_password

        response = super().form_valid(form)
        self.request.user.must_change_password = False
        self.request.user.save(update_fields=["must_change_password"])
        messages.success(self.request, "Your password has been updated.")

        # on_commit, not a direct call: this view is not wrapped in a
        # transaction we control, but if it ever is, mail must not go out for a
        # change that rolled back. The task also gets a plain pk rather than the
        # user instance, so nothing keeps the request alive in the broker.
        transaction.on_commit(
            lambda: notify_user_of_password_change.delay(
                self.request.user.pk, was_first_run
            )
        )
        return response

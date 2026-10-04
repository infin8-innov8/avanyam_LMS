"""Views for registration, login, the admin approval queue, and role management.

Every authorization question is delegated to `policies` (§7). No view inspects
`user.is_trainer` directly.

Three entry points create an application, and they differ only in *who* fills the
form in — never in what the application looks like:

* `/accounts/signup/` — the person themselves.
* `/accounts/create/` — a Trainer or Admin entering an account for somebody else
  (`SignupRequest.created_by` records which it was).

Both post to `SignupForm` and both go through `service.signup`, so the pending
account, the requested role and the notification cannot diverge between them.
"""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.contrib.auth import logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, PasswordChangeView
from django.db import transaction
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from apps.accounts.domain.enums import ApprovalStatus, RequestedRole
from apps.accounts.forms import LoginForm, SignupForm
from apps.accounts.models import (
    APPROVAL_UNDO_LIFETIME_MINUTES,
    ApprovalUndoToken,
    SignupRequest,
    User,
)
from apps.accounts.policies import (
    can_manage_roles,
    can_register,
    can_undo,
    can_view_queue,
    can_view_request,
    is_approved,
    visible_requests,
)
from apps.accounts.service.approval import Decision, DecisionError, decide
from apps.accounts.service.roles import RoleError, current_roles, set_role
from apps.accounts.service.signup import signup_enabled
from apps.accounts.service.undo import UndoError, request_undo_code, undo_rejection
from apps.common.logging import get_logger
from config.observability import describe_user

logger = get_logger(__name__)


class PortalLoginView(LoginView):
    """Branded login. An account that is not approved cannot sign in at all.

    `prd.md` §5.1 requires `PENDING_APPROVAL` accounts to fail on **every**
    backend, so the refusal happens before a session is created rather than after
    (D50). The earlier behaviour authenticated the person and showed them a
    waiting page, which meant two outcomes for "correct password, not approved"
    and gave a pending user a live session to be chased out of.

    Two consequences of how Django gets there, worth writing down because they
    are invisible from the code below:

    * An inactive account is refused by `ModelBackend.user_can_authenticate`
      returning `None` *inside* `AuthenticationForm.clean()`, so `form_valid` is
      never reached. The `is_approved` check there is a backstop for an account
      that somehow ended up active while unapproved, not the live path.
    * That means the refusal arrives indistinguishable from a wrong password. The
      form cannot say "you are pending" without confirming the address is
      registered, so it does not; `form_invalid` recovers the reason for the log
      instead, where confirming it costs nothing.
    """

    template_name = "accounts/login.html"
    authentication_form = LoginForm
    redirect_authenticated_user = False

    def form_valid(self, form):
        # `form.get_user()` is the authenticated user, available before
        # `super()` writes the session. Checking here rather than after is what
        # makes the refusal session-free.
        #
        # Reachable only if a backend admitted an unapproved account, since an
        # inactive one never authenticates in the first place. Kept because the
        # cost of being wrong here is an unapproved person holding a session, and
        # the cost of the check is one attribute read.
        user = form.get_user()
        if not is_approved(user):
            form.add_error(
                None,
                "Your account is waiting for an administrator to approve it. "
                "You will be able to sign in once that is done.",
            )
            return self.form_invalid(form, reason="account_not_approved")

        response = super().form_valid(form)
        # `describe_user` because the actor fields are otherwise assembled in two
        # other places, and a login event that spells them differently from the
        # access log is a login event nobody can query.
        logger.info(
            "auth.login_succeeded",
            "Sign-in accepted",
            outcome="success",
            account_approved=True,
            **describe_user(user),
        )
        return response

    def form_invalid(self, form, reason: str = "invalid_credentials"):
        # The reason a login failed is the most security-relevant event this app
        # produces, and the least visible by default. `django.request` only sees
        # the 200 that the re-rendered form produces -- there is no 401 to alert on.
        # No address, no password, no submitted value: the identifier is already in
        # the request context if it is needed.
        actor = {}
        if reason == "invalid_credentials":
            # A pending applicant with the right password lands here looking
            # exactly like a typo, because the backend dropped them before the
            # form could tell them apart. The caller is not told, because that
            # would confirm the address is registered; the log is, because
            # "nobody is working the queue" and "someone is guessing passwords"
            # need opposite responses and are otherwise one metric apart.
            unapproved = self._unapproved_account(form)
            if unapproved is not None:
                reason = "account_not_approved"
                actor = describe_user(unapproved)

        logger.warning(
            "auth.login_refused",
            "Sign-in was refused",
            outcome="refused",
            reason=reason,
            errors=sorted(form.errors.keys()),
            **actor,
        )
        return super().form_invalid(form)

    @staticmethod
    def _unapproved_account(form) -> User | None:
        """The account behind a failed sign-in, if it exists, is unapproved, and the password was right.

        One extra lookup and one hash check, both only on the failure path. A
        caller who is guessing gets nothing back either way -- the form
        re-renders identically -- so this is for the log's benefit alone.

        The password check is what makes the reason honest. Without it a plain
        typo against a pending address would be logged as `account_not_approved`,
        which overstates: nobody with the wrong password should be counted as
        waiting on a queue. With it, `account_not_approved` means exactly what it
        says -- correct password, account not approved.

        Read from `form.data` rather than `cleaned_data`: a failed `clean()` never
        populates it. The key is Django's conventional `username` field, which this
        project's `LoginForm` keeps even though it is labelled "Email", and
        `add_prefix` is what the template will have used to render it.
        """
        data = form.data
        address = (data.get(form.add_prefix("username")) or "").strip()
        password = data.get(form.add_prefix("password")) or ""
        if not address or not password:
            return None

        candidate = (
            User.objects.filter(email__iexact=address)
            .exclude(approval_status=ApprovalStatus.APPROVED)
            .first()
        )
        if candidate is None or not candidate.check_password(password):
            return None
        return candidate

    def get_success_url(self) -> str:
        return self.get_redirect_url() or reverse_lazy("accounts:home")


def _submit_or_attach_refusal(form: forms.Form, requester: User | None):
    """Run `SignupForm.save()`, turning a service refusal into a form error.

    `is_valid()` has already passed by the time we get here, but the service
    re-checks authority -- it has to, because it is also called from
    `manage.py` and from tests. Those two checks cannot both be the only one.

    A Trainee POSTing to `/accounts/signup/` is the concrete case: the view lets an
    authenticated POST through so staff can register other people, the form
    validates, and only `submit_application` knows the requester has no
    authority. Its `SignupError` arrives as a `ValidationError` out of `save()`,
    and this function is a plain view -- not a `FormView` -- so nothing catches it.
    Unhandled, that is a 500 with a traceback page: an ordinary refusal answering
    as a server fault, and a real crash on a public endpoint.
    """
    try:
        return form.save(requester=requester), None
    except forms.ValidationError as exc:
        form.add_error(None, exc)
        return None, form


def signup(request):
    # Only bounce an already-signed-in visitor on GET. Redirecting an
    # authenticated POST would silently discard the submission and return a 302
    # that looks like success -- a Trainer creating an account for a trainee would
    # see "request received" with nothing created.
    if request.user.is_authenticated and request.method == "GET":
        return redirect("accounts:home")
    if not signup_enabled():
        return render(
            request,
            "accounts/signup_closed.html",
            {"support_email": "avanyam.official@gmail.com"},
            status=503,
        )

    form = SignupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        on_behalf_of = request.user if request.user.is_authenticated else None
        outcome = _submit_or_attach_refusal(form, on_behalf_of)
        if outcome[1] is not None:
            return render(
                request,
                "accounts/signup.html",
                {"form": outcome[1], "signup_enabled": True},
                status=403,
            )
        result = outcome[0]
        # One application event, so a signup spike sits next to the approval events
        # it creates. The applicant's address is deliberately absent -- a signup
        # flood is the sort of thing that ends up on a shared dashboard, and the row
        # already carries the address for anyone who goes looking.
        logger.info(
            "auth.signup_received",
            "A registration application was submitted",
            outcome="success",
            request_pk=str(result.request.pk),
            new_user_pk=str(result.user.pk),
            requested_role=result.request.requested_role,
            submitted_by_staff=on_behalf_of is not None,
            **describe_user(on_behalf_of),
        )
        return redirect("accounts:applied")

    return render(
        request,
        "accounts/signup.html",
        {"form": form, "signup_enabled": True},
    )


@login_required
@require_http_methods(["GET", "POST"])
def create_account(request):
    """A Trainer or Admin registering somebody else.

    Same form, same service, same pending outcome as self-registration. What
    differs is recorded, not branched: `created_by` on the application says a
    staff member entered it, and the applicant still has to be approved by an
    Admin before they can sign in.
    """
    verdict = can_register(request.user)
    if not verdict.allowed:
        return render(
            request,
            "accounts/no_queue.html",
            {"pending_state": request.user.approval_status, "reason": verdict.reason},
            status=403,
        )

    form = SignupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        outcome = _submit_or_attach_refusal(form, request.user)
        if outcome[1] is not None:
            return render(
                request,
                "accounts/create_account.html",
                {"form": outcome[1]},
                status=403,
            )
        result = outcome[0]
        logger.info(
            "auth.account_created_by_staff",
            "A staff member registered an account for somebody else",
            outcome="success",
            request_pk=str(result.request.pk),
            new_user_pk=str(result.user.pk),
            requested_role=result.request.requested_role,
            **describe_user(request.user),
        )
        messages.success(
            request,
            f"An application for {result.request.full_name} was created and is "
            "waiting for an administrator to approve it.",
        )
        return redirect("accounts:home")

    return render(
        request,
        "accounts/create_account.html",
        {
            "form": form,
            "role_choices": [(r.value, r.value.capitalize()) for r in RequestedRole],
        },
    )


def applied(request):
    return render(request, "accounts/applied.html")


#: The status tabs offered on the approval queue, in display order. `all` is
#: reachable by hand but not offered, because mixing pending with decided
#: history is the thing this page was redesigned to stop doing.
QUEUE_FILTERS = ("pending", "approved", "rejected", "redirected")

#: Tab labels. A status name is not a sentence an admin should have to decode,
#: and "redirected" in particular means nothing to someone who did not choose it.
QUEUE_FILTER_LABELS = {
    "pending": "Pending",
    "approved": "Approved",
    "rejected": "Declined",
    "redirected": "Told to try again",
}


@login_required
def home(request):
    """Where every approved user lands.

    A pending account cannot get here: `PortalLoginView` refuses one at sign-in,
    so `is_approved` is not a branch this page needs. It is still passed, because
    an LDAP account approved by a directory sync rather than by the queue is a
    legitimate way to be signed in and approved, and the template saying nothing
    about it would read as an omission.
    """
    return render(
        request,
        "accounts/home.html",
        {
            "user_obj": request.user,
            "is_approved": request.user.is_approved,
            "roles": current_roles(request.user),
        },
    )


@login_required
def admin_queue(request):
    """Every application awaiting a decision. Admins only.

    Access is a policy call, and the list is filtered by the same policy, so an
    admin can never see -- let alone act on -- an application by guessing a UUID
    in the URL.

    A signed-in non-admin still gets 404 rather than 403, because a 403 would
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
    # the wrong default: the one thing an admin opened this page to do is buried
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
    # that renders for everyone and 403s on submit teaches admins that the
    # queue lies.
    #
    # Set as an attribute at the row rather than collected into a dict keyed by
    # pk: Django cannot do `dict[variable]` in a template, and the workaround for
    # that (a custom `get_item` filter) is a new template tag to express one
    # lookup. The attribute rides along on the instance the template already has.
    for row in shown:
        row.undo_allowed = (
            can_view_request(request.user, row).allowed and can_undo(row).allowed
        )

    # Rows where this viewer already has a live code waiting. The dialog opens
    # straight on the code box for these rather than mailing a second code, which
    # would expire the one they are reading. The remaining life rides along too, so
    # the countdown on screen starts from the server's number instead of the ten
    # minutes this page was rendered at -- otherwise a queue left open for an hour
    # would offer an hour more than the token actually has.
    undoable = [row.pk for row in shown if row.undo_allowed]
    expiry_by_request: dict = {}
    if undoable:
        now = timezone.now()
        expiry_by_request = {
            request_id: max(int((expires_at - now).total_seconds()), 0)
            for request_id, expires_at in _live_codes(
                request.user, undoable
            ).values_list("request_id", "expires_at")
        }
    for row in shown:
        remaining = expiry_by_request.get(row.pk)
        row.code_sent = remaining is not None
        row.code_seconds_left = remaining or 0

    return render(
        request,
        "accounts/admin_queue.html",
        {
            "requests": shown,
            "tabs": tabs,
            "counts": counts,
            "total": len(rows),
            "selected": selected,
            "role_choices": [(r.value, r.value.capitalize()) for r in RequestedRole],
        },
    )


#: A live code is one this viewer requested, that has not been used, and that has
#: not expired. Built as a filter rather than a predicate so the queue can ask
#: about every row in one query instead of one per row.
def _live_codes(admin, request_ids):
    return ApprovalUndoToken.objects.filter(
        request_id__in=request_ids,
        requested_by=admin,
        consumed_at__isnull=True,
        expires_at__gt=timezone.now(),
    )


@login_required
def role_management(request):
    """Admin-only list of accounts, with the role control on each row.

    Not a filter on the approval queue: that queue is about undecided
    applications, this is about people who already have access. Combining them
    makes one page do two unrelated jobs.
    """
    verdict = can_manage_roles(request.user)
    if not verdict.allowed:
        return render(
            request,
            "accounts/no_queue.html",
            {"pending_state": request.user.approval_status, "reason": verdict.reason},
            status=404,
        )

    people = User.objects.prefetch_related("role_assignments__role").order_by(
        "full_name", "email"
    )
    return render(
        request,
        "accounts/role_management.html",
        {
            "people": people,
            "role_choices": [(r.value, r.value.capitalize()) for r in RequestedRole],
        },
    )


@login_required
@require_POST
def set_user_role(request, user_pk):
    """Set exactly one role on ``user_pk`` -- the promote/demote action (D49)."""
    target = get_object_or_404(User, pk=user_pk)
    slug = request.POST.get("role", "")
    try:
        outcome = set_role(actor=request.user, subject=target, slug=slug)
    except RoleError as exc:
        messages.error(request, str(exc))
        return redirect("accounts:role-management")

    summary = ", ".join(outcome.changed)
    logger.info(
        "roles.changed_by_admin",
        "An admin changed an account role",
        outcome="success",
        subject_pk=str(target.pk),
        role=outcome.role,
        changed=list(outcome.changed),
        **describe_user(request.user),
    )
    # Reported as what actually happened rather than as the destination. "You are
    # now a Trainee" is wrong on a demotion *into* trainee that touched two rows,
    # and the admin who just did it wants to know a trainer row was cleared.
    messages.success(
        request,
        f"{target.full_name}'s role changed: {summary}."
        if summary
        else f"{target.full_name} was already a {outcome.role.capitalize()}.",
    )
    return redirect("accounts:role-management")


@login_required
@require_POST
def decide_request(request, request_pk):
    # Passed through as-is rather than collapsed to `approve = ... == "approve"`.
    # That comparison made every non-approve value a rejection, so the redirect
    # option could not have worked even once the service supported it.
    posted = request.POST.get("decision", "")
    override = request.POST.get("role") or None
    try:
        outcome = decide(
            approver=request.user,
            request_pk=request_pk,
            decision=posted,
            note=request.POST.get("note", "")[:2000],
            role=override,
        )
    except SignupRequest.DoesNotExist as exc:
        raise Http404("No such application.") from exc
    except DecisionError as exc:
        messages.error(request, str(exc))
        return redirect("accounts:admin-queue")

    messages.success(
        request,
        f"{outcome.request.full_name} was {_DECISION_PAST_TENSE[posted]}.",
    )
    return redirect("accounts:admin-queue")


#: What to tell the admin they just did. Keyed by the posted value, so an
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
    return redirect("accounts:admin-queue")


@login_required
def undo_request_page(request, request_pk):
    """The undo flow as a page of its own, for admins without JS.

    The queue row carries one button and nothing else. With JS the dialog does
    both steps in place; without it, posting the button lands here, which is the
    only other place the code box appears. Keeping it off the queue is the point:
    a second control beside "Undo decline" turned one action into a choice between
    two, and let an admin submit an empty code before any existed.
    """
    req = get_object_or_404(
        SignupRequest.objects.select_related("user", "decided_by"),
        pk=request_pk,
    )
    visible = can_view_request(request.user, req).allowed
    if not (visible and can_undo(req).allowed):
        raise Http404("No such application.")

    live = _live_codes(request.user, [req.pk]).order_by("-created_at").first()
    remaining = (
        max(int((live.expires_at - timezone.now()).total_seconds()), 0)
        if live is not None
        else APPROVAL_UNDO_LIFETIME_MINUTES * 60
    )
    return render(
        request,
        "accounts/undo_request.html",
        {
            "req": req,
            "code_sent": live is not None,
            # The token's real remaining life, not the full window. Printing the
            # full ten minutes on a page opened nine minutes in would send the
            # admin away waiting for time the token no longer has, and the code
            # would come back expired.
            "seconds_left": remaining,
            "minutes_left": max((remaining + 59) // 60, 1),
        },
    )


@login_required
@require_POST
def request_undo_code_view(request, request_pk):
    """Mail the admin a code so they can undo a decline."""
    try:
        issued = request_undo_code(actor=request.user, request_pk=request_pk)
    except SignupRequest.DoesNotExist as exc:
        raise Http404("No such application.") from exc
    except UndoError as exc:
        return _undo_refused(request, str(exc))

    minutes = APPROVAL_UNDO_LIFETIME_MINUTES
    remaining = int((issued.token.expires_at - timezone.now()).total_seconds())
    if _wants_json(request):
        # `sent`, and it means it: `deliver_undo_code` ran inside this request and
        # an SMTP failure would have come back as a refusal above, not as a 200.
        # The countdown is seeded from the token's real remaining life so the clock
        # on screen and the expiry the server enforces are the same number.
        #
        # `email` is the admin's own address, returned so the dialog can say where
        # the code went. It is the address the request authenticated as, so this
        # discloses nothing the page does not already know.
        return JsonResponse(
            {
                "ok": True,
                "sent": True,
                "email": issued.token.requested_by.email,
                "seconds_left": max(remaining, 0),
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
            actor=request.user,
            request_pk=request_pk,
            code=request.POST.get("code", ""),
        )
    except SignupRequest.DoesNotExist as exc:
        raise Http404("No such application.") from exc
    except UndoError as exc:
        # A refused code leaves the application rejected -- the service only moves
        # the row inside `_redeem`, and nothing was redeemed. The dialog stays on
        # its second step so the admin can try again or ask for a new code.
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
    return redirect("accounts:admin-queue")


def logout_view(request):
    # Read the actor before `auth_logout`, which flushes the session and leaves
    # `request.user` anonymous -- after it, there is nobody left to describe.
    who = describe_user(request.user)
    auth_logout(request)
    logger.info("auth.logged_out", "Sign-out completed", outcome="success", **who)
    return redirect("accounts:login")


class PasswordChangeViewForUser(PasswordChangeView):
    """The ordinary "change your own password" screen.

    It used to also clear `must_change_password`, which existed to contain
    accounts issued a placeholder password. Nobody is issued one any more, so the
    flag and the redirect that policed it are gone (D43) and this is a plain
    `PasswordChangeView` that still emails the security notice.
    """

    template_name = "accounts/password_change.html"
    success_url = reverse_lazy("accounts:home")

    def form_valid(self, form):
        # Imported here, not at module scope, matching signup.py and approval.py:
        # tasks imports models, and views is imported by tasks' own module graph
        # via the URLconf.
        from apps.accounts.tasks import notify_user_of_password_change

        who = describe_user(self.request.user)
        user_pk = self.request.user.pk

        response = super().form_valid(form)
        logger.info("auth.password_changed", "Password was changed", outcome="success", **who)
        messages.success(self.request, "Your password has been updated.")

        # on_commit, not a direct call: this view is not wrapped in a
        # transaction we control, but if it ever is, mail must not go out for a
        # change that rolled back. The task also gets a plain pk rather than the
        # user instance, so nothing keeps the request alive in the broker.
        transaction.on_commit(lambda: notify_user_of_password_change.delay(user_pk))
        return response
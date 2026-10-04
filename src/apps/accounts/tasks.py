"""Celery tasks for approval email.

Email is *deferred to a worker*, not sent inline in the request, for two reasons:
a slow SMTP handshake (Gmail's can take seconds) would hold the trainee's HTTP
request open, and the SMTP relay is a shared single point of failure that should
not be able to roll back a signup.

`acks_late` + bounded retry keeps a transient SMTP hiccup from silently losing an
approval notice.

Retry policy is deliberately NOT `autoretry_for=(Exception,)`. That was a bug: it
retried failures that can never succeed on a retry --

  * `SignupRequest.DoesNotExist` -- a stale task id, five pointless attempts
  * `SMTPRecipientsRefused` / `SMTPSenderRefused` -- the mailbox is gone; Gmail
    caps consumer sending at ~500/day, so burning quota on a dead address is how
    real mail stops being delivered

Listing `smtplib.SMTPException` instead was *also* wrong, and subtler. Celery
matches `autoretry_for` with `isinstance`, and the smtplib hierarchy is inverted
from what it looks like:

    SMTPException
    +-- SMTPResponseException
    |   +-- SMTPRecipientsRefused   550 no such user      <- permanent
    |   +-- SMTPSenderRefused       553 not allowed       <- permanent
    +-- SMTPDataError              552 message too big   <- permanent
    +-- SMTPServerDisconnected     dropped mid-session    <- transient
    +-- SMTPConnectError           421 not accepting      <- transient
    +-- SMTPHeloError              450 greylisting        <- transient

So catching the base class retried exactly the permanent failures the narrower
list was meant to exclude. `RETRYABLE` therefore names the transient leaves
individually; `test_retry_policy_matches_the_documented_intent` asserts it.
"""

from __future__ import annotations

import smtplib
import socket

from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db.models import Q
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import strip_tags

from apps.common.logging import get_logger

logger = get_logger(__name__)

#: Failures worth retrying, named as leaves rather than caught by their base
#: class. Everything omitted -- bad recipient, missing row, template bug -- fails
#: identically on every attempt, so retrying only delays the real error.
#:
#: Note `socket.timeout` is `TimeoutError` on Python 3.10+ and `socket.gaierror`
#: is an `OSError`, so neither is covered by `ConnectionError`.
RETRYABLE: tuple[type[BaseException], ...] = (
    smtplib.SMTPServerDisconnected,  # connection dropped mid-session
    smtplib.SMTPConnectError,  # 421, server not accepting mail right now
    smtplib.SMTPHeloError,  # 450, greylisting
    socket.timeout,
    socket.gaierror,  # DNS failure
    ConnectionError,  # reset / refused / broken pipe
)

#: Explicitly permanent. Listed for documentation and for the test that pins the
#: policy; deliberately NOT part of RETRYABLE.
NOT_RETRYABLE: tuple[type[BaseException], ...] = (
    smtplib.SMTPRecipientsRefused,
    smtplib.SMTPSenderRefused,
    smtplib.SMTPDataError,
    smtplib.SMTPNotSupportedError,
)

RETRY_KWARGS = {
    "autoretry_for": RETRYABLE,
    "retry_backoff": True,
    "retry_backoff_max": 600,
    "retry_jitter": True,
    "max_retries": 5,
}


def _send(
    subject: str,
    text_body: str,
    html_body: str,
    recipients: list[str],
    on_sent=None,
) -> int:
    """Send one multipart message. Returns the number of messages handed over.

    ``on_sent`` runs only after the mail is accepted, so callers can record
    "this actually went out" as distinct from "this was handed to the broker".
    """
    if not recipients:
        logger.warning(
            "mail.send_skipped",
            "No recipients resolved, so nothing was sent",
            outcome="skipped",
            subject=subject,
            recipients=0,
        )
        return 0
    message = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=recipients,
    )
    message.attach_alternative(html_body, "text/html")
    # fail_silently=False: a task that "succeeds" while dropping mail is worse
    # than a task that retries and shows up in the failure log.
    sent = message.send(fail_silently=False)
    if sent and on_sent is not None:
        on_sent()
    # Recorded here rather than at each of the six call sites, so `mail.log` is a
    # complete record of what this app handed to the mail server. Every call site
    # also logs its own domain event -- `undo.code_sent` and friends -- but those
    # say what the *business* event was; this says the mail actually went out, and
    # the two can disagree (a send that succeeds while `on_sent` then raises is the
    # interesting case, and only these two lines apart can show it).
    #
    # The recipient count, never the address. `mail.log` is the file most likely to
    # be shipped to a third-party mail provider for debugging.
    logger.info(
        "mail.sent",
        "Message handed to the mail server",
        outcome="success",
        subject=subject,
        recipients=len(recipients),
        backend=settings.EMAIL_BACKEND.split(".")[-1],
    )
    return sent


def _mark_emailed(token_pk: str) -> None:
    """Stamp the token as mailed, so the UI can stop waiting on it.

    Best-effort on purpose. The mail is already gone by this point, so failing to
    write the stamp must not turn a delivered code into a retried one -- that
    would mail the admin a second code and expire the first.
    """
    from apps.accounts.models import ApprovalUndoToken

    try:
        ApprovalUndoToken.objects.filter(pk=token_pk).update(
            emailed_at=timezone.now()
        )
    except Exception:  # pragma: no cover - defensive
        logger.exception(
            "undo.token_stamp_failed",
            "Code was delivered but emailed_at could not be recorded",
            outcome="failure",
            token_pk=token_pk,
        )


def admin_recipient_addresses() -> list[str]:
    """Every address entitled to hear that an application is waiting (D41).

    Split out of `notify_admins_of_application` for two reasons. It is the whole
    "who gets notified" rule, which is worth naming and testing on its own; and
    the suite runs against a long-lived development database that already holds
    real admins, so a test cannot assert on an empty recipient set by arranging
    state. With the seam exposed, the no-admin case is patched rather than staged.

    Superusers are included alongside accounts holding the `admin` Role. Django's
    own `createsuperuser` grants no Role, and excluding them here would mean the
    person on call gets no notice of a pending application. That matches
    `policies.is_superuser`, which honours the same override.

    `is_active` and `approval_status` are both filtered because an admin row on a
    half-created or suspended account confers nothing -- the same reasoning as
    `policies.is_admin`.
    """
    from apps.accounts.domain.enums import ApprovalStatus
    from apps.accounts.models import ROLE_ADMIN, User

    admins = (
        User.objects.filter(is_active=True, approval_status=ApprovalStatus.APPROVED)
        .filter(Q(role_assignments__role__slug=ROLE_ADMIN) | Q(is_superuser=True))
        .distinct()
        .order_by("pk")
        .values_list("email", flat=True)
    )
    return [address for address in admins if address]


@shared_task(name="accounts.notify_admins_of_application", **RETRY_KWARGS)
def notify_admins_of_application(request_pk: str) -> int:
    """Tell every admin that a registration application is waiting (D41).

    Replaces `notify_trainer_of_application`, which mailed one nominated trainer.
    Under the single shared queue an applicant no longer names anyone, so "who to
    notify" became "everybody who can decide" -- see :func:`admin_recipient_addresses`.
    """
    from apps.accounts.models import SignupRequest

    req = SignupRequest.objects.select_related("user", "created_by").get(pk=request_pk)

    recipients = admin_recipient_addresses()

    if not recipients:
        # Not an error: a deployment with no admin yet has a real problem, but it
        # is a `manage.py createadmin` away from being fixed, and the application
        # is not going to be processed by retrying this mail. Logged at error
        # level because it is still the reason nobody has approved this row.
        logger.error(
            "mail.send_skipped",
            "No active admin exists, so the application was not announced",
            outcome="skipped",
            request_pk=req.pk,
            subject="application",
        )
        return 0

    ctx = {
        "request": req,
        "portal_url": f"{settings.SITE_URL.rstrip('/')}/accounts/admin/queue/",
        "created_by": req.created_by,
    }
    subject = f"[Avanyam] New registration from {req.full_name}"
    return _send(
        subject,
        strip_tags(render_to_string("accounts/email/admin_new_application.txt", ctx)),
        render_to_string("accounts/email/admin_new_application.html", ctx),
        recipients,
    )


@shared_task(name="accounts.notify_applicant_of_decision", **RETRY_KWARGS)
def notify_applicant_of_decision(request_pk: str) -> int:
    """Tell the applicant their application was approved or declined."""
    from apps.accounts.domain.enums import ApprovalStatus
    from apps.accounts.models import SignupRequest
    from apps.accounts.service.roles import current_roles

    req = SignupRequest.objects.select_related("user", "decided_by").get(pk=request_pk)
    login_url = f"{settings.SITE_URL.rstrip('/')}/accounts/login/"
    signup_url = f"{settings.SITE_URL.rstrip('/')}/accounts/signup/"
    approved = req.status == ApprovalStatus.APPROVED

    # A redirect is a decline that explicitly permits another application, so it
    # must not be described as a permanent refusal. Telling someone they may not
    # register again when they may is the sort of error that costs a support
    # thread and a re-read of the form.
    redirected = req.status == ApprovalStatus.REDIRECTED

    # Read the live RoleAssignment rather than a value stashed on the request, so
    # this stays correct after an admin promotes or demotes the account later and
    # the mail -- which arrives after the transaction -- still describes the role
    # the person actually has. `requested_role` is what they asked for, which is
    # not necessarily what they got.
    granted = current_roles(req.user) if (approved and req.user_id) else []
    ctx = {
        "request": req,
        "approved": approved,
        "redirected": redirected,
        "login_url": login_url,
        "signup_url": signup_url,
        "granted_roles": granted,
        "granted_role": granted[0] if granted else "",
        "role_overridden": bool(
            granted and granted[0] != req.requested_role
        ),
        "decided_by": req.decided_by,
    }
    if approved:
        subject = "[Avanyam] Your registration is approved"
    elif redirected:
        subject = "[Avanyam] Your registration was not approved this time"
    else:
        subject = "[Avanyam] Your registration was not approved"
    return _send(
        subject,
        strip_tags(render_to_string("accounts/email/applicant_decision.txt", ctx)),
        render_to_string("accounts/email/applicant_decision.html", ctx),
        [req.email],
    )


def deliver_undo_code(token_pk: str, code: str) -> int:
    """Mail an admin the code needed to reverse a rejection they made.

    Deliberately a plain function, not the task. This is the one notification the
    interface has to be honest about: the dialog says the code is on its way, so
    the answer has to mean the SMTP server took the message. Handing it to a queue
    only proves the broker accepted it, which is how a worker running stale code
    could leave the admin waiting on an inbox that was never going to ring. The
    cost is one SMTP round trip inside the request -- about a second -- and in
    exchange the UI never has to poll, guess, or apologise.

    The other notifications in this module are still queued. Nothing is waiting on
    them, so there is no reason to hold a web worker open for them.
    """
    from apps.accounts.models import APPROVAL_UNDO_LIFETIME_MINUTES, ApprovalUndoToken

    token = ApprovalUndoToken.objects.select_related(
        "request", "requested_by"
    ).get(pk=token_pk)
    req = token.request
    admin = token.requested_by

    if admin is None:
        logger.error(
            "undo.code_send_skipped",
            "Undo token has no requester, so no code can be sent",
            outcome="skipped",
            token_pk=token_pk,
            request_pk=req.pk,
        )
        return 0

    # The applicant's address is never included. A rejection reversal is the
    # admin's own correction to make; copying the person into that thread would
    # tell them a rejection is being quietly undone.
    ctx = {
        "request": req,
        "admin": admin,
        "code": code,
        "minutes": APPROVAL_UNDO_LIFETIME_MINUTES,
        "queue_url": (
            f"{settings.SITE_URL.rstrip('/')}/accounts/admin/queue/?filter=rejected"
        ),
    }
    return _send(
        f"[Avanyam] Code to reverse the rejection of {req.full_name}",
        strip_tags(render_to_string("accounts/email/undo_code.txt", ctx)),
        render_to_string("accounts/email/undo_code.html", ctx),
        [admin.email],
        on_sent=lambda: _mark_emailed(token_pk),
    )


@shared_task(name="accounts.notify_admin_of_undo_code", **RETRY_KWARGS)
def notify_admin_of_undo_code(token_pk: str, code: str) -> int:
    """Queue wrapper around `deliver_undo_code`, for parity with its siblings.

    Kept because an operator may want to re-send by hand from a shell, and because
    the task name is what a broker-side retry would address. The interactive path
    calls `deliver_undo_code` directly -- see its docstring for why.

    The code travels as an argument rather than being re-read from the token,
    because the token only holds an HMAC. That means it sits in the broker payload,
    so `CELERY_TASK_SERIALIZER` matters here: it must be JSON, never pickle. See
    the security note in `config/settings/base.py`.
    """
    return deliver_undo_code(token_pk, code)


@shared_task(name="accounts.notify_applicant_of_reinstatement", **RETRY_KWARGS)
def notify_applicant_of_reinstatement(request_pk: str) -> int:
    """Tell an applicant their rejection was reversed and they are queued again.

    Deliberately says who did it. A person who was told "you are permanently
    declined" and then receives an unexplained "you are in the queue" email has
    been given no way to judge whether the reversal is legitimate.
    """
    from apps.accounts.domain.enums import ApprovalStatus
    from apps.accounts.models import SignupRequest

    req = SignupRequest.objects.select_related("user", "decided_by").get(
        pk=request_pk
    )
    if req.status != ApprovalStatus.PENDING:
        # Raced with another decision, or the undo was rolled back after the mail
        # was queued. Saying "you are in the queue" when they are not would be
        # worse than saying nothing.
        logger.warning(
            "mail.send_skipped",
            "Request is no longer pending, so no reinstatement notice is sent",
            outcome="skipped",
            request_pk=request_pk,
            status=req.status,
            subject="reinstatement",
        )
        return 0

    ctx = {
        "request": req,
        "admin": req.decided_by,
        "queue_url": f"{settings.SITE_URL.rstrip('/')}/accounts/login/",
    }
    return _send(
        "[Avanyam] Your rejection was reversed -- you are back in the queue",
        strip_tags(render_to_string("accounts/email/applicant_reinstatement.txt", ctx)),
        render_to_string("accounts/email/applicant_reinstatement.html", ctx),
        [req.email],
    )


@shared_task(name="accounts.notify_user_of_password_change", **RETRY_KWARGS)
def notify_user_of_password_change(user_pk: int) -> int:
    """Tell someone their password just changed.

    This is the one notification in the system aimed at protecting an account
    rather than informing about course business, so it is worded as a security
    notice and never contains the password, a reset link, or the old value.

    There is no second wording for a first sign-in. It used to take a
    `via_first_run` flag because accounts were issued a placeholder password and
    had to be forced through a change; nobody is issued one any more (D43), so the
    flag had one remaining caller shape and no remaining meaning. Every change now
    gets the same notice, which is also the safer default: a message that varies by
    unknown origin is a message that can be spoofed into looking routine.
    """
    from apps.accounts.models import User

    user = User.objects.get(pk=user_pk)
    login_url = f"{settings.SITE_URL.rstrip('/')}/accounts/login/"

    # A locked or deactivated account getting a password notice is noise, and
    # mail to an address we no longer consider usable invites a support thread.
    if not user.is_active or not user.email:
        logger.warning(
            "mail.send_skipped",
            "User is inactive or has no email, so no notice was sent",
            outcome="skipped",
            user_pk=user_pk,
            subject="password_change",
        )
        return 0

    ctx = {
        "user": user,
        "login_url": login_url,
        "changed_at": timezone.now(),
    }
    return _send(
        "[Avanyam] Your password was changed",
        strip_tags(render_to_string("accounts/email/password_changed.txt", ctx)),
        render_to_string("accounts/email/password_changed.html", ctx),
        [user.email],
    )

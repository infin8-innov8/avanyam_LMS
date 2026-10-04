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

import logging
import smtplib
import socket

from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import strip_tags

logger = logging.getLogger(__name__)

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


def _send(subject: str, text_body: str, html_body: str, recipients: list[str]) -> int:
    if not recipients:
        logger.warning("no recipients for %r; nothing sent", subject)
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
    return message.send(fail_silently=False)


@shared_task(name="accounts.notify_trainer_of_application", **RETRY_KWARGS)
def notify_trainer_of_application(request_pk: str) -> int:
    """Tell the *chosen* trainer that someone wants to register."""
    from apps.accounts.models import SignupRequest

    req = SignupRequest.objects.select_related("selected_trainer").get(pk=request_pk)
    trainer = req.selected_trainer
    if trainer is None:
        logger.error("signup %s has no trainer; cannot notify", req.pk)
        return 0

    ctx = {
        "request": req,
        "trainer": trainer,
        "portal_url": f"{settings.SITE_URL.rstrip('/')}/accounts/trainer/queue/",
    }
    subject = f"[Avanyam] New registration from {req.full_name}"
    return _send(
        subject,
        strip_tags(render_to_string("accounts/email/trainer_new_application.txt", ctx)),
        render_to_string("accounts/email/trainer_new_application.html", ctx),
        [trainer.email],
    )


@shared_task(name="accounts.notify_applicant_of_decision", **RETRY_KWARGS)
def notify_applicant_of_decision(request_pk: str) -> int:
    """Tell the applicant their application was approved or declined."""
    from apps.accounts.domain.enums import ApprovalStatus
    from apps.accounts.models import SignupRequest

    req = SignupRequest.objects.select_related("selected_trainer", "decided_by").get(
        pk=request_pk
    )
    login_url = f"{settings.SITE_URL.rstrip('/')}/accounts/login/"
    signup_url = f"{settings.SITE_URL.rstrip('/')}/accounts/signup/"
    approved = req.status == ApprovalStatus.APPROVED

    # A redirect is a decline that explicitly permits another application, so it
    # must not be described as a permanent refusal. Telling someone they may not
    # register again when they may is the sort of error that costs a support
    # thread and a re-read of the form.
    redirected = req.status == ApprovalStatus.REDIRECTED
    ctx = {
        "request": req,
        "approved": approved,
        "redirected": redirected,
        "login_url": login_url,
        "signup_url": signup_url,
        "trainer": req.selected_trainer,
        "decided_by": req.decided_by,
    }
    if approved:
        subject = "[Avanyam] Your registration is approved"
    elif redirected:
        subject = "[Avanyam] You can register again with a different trainer"
    else:
        subject = "[Avanyam] Your registration was not approved"
    return _send(
        subject,
        strip_tags(render_to_string("accounts/email/applicant_decision.txt", ctx)),
        render_to_string("accounts/email/applicant_decision.html", ctx),
        [req.email],
    )


@shared_task(name="accounts.notify_trainer_of_undo_code", **RETRY_KWARGS)
def notify_trainer_of_undo_code(token_pk: str, code: str) -> int:
    """Mail a trainer the code needed to reverse a rejection they made.

    The code travels as a task argument rather than being re-read from the token,
    because the token only holds an HMAC. That means it sits in the broker
    payload, so `CELERY_TASK_SERIALIZER` matters here more than anywhere else in
    this module: it must be JSON, never pickle. See the security note in
    `config/settings/base.py`.
    """
    from apps.accounts.domain.enums import ApprovalStatus
    from apps.accounts.models import APPROVAL_UNDO_LIFETIME_MINUTES, ApprovalUndoToken

    token = ApprovalUndoToken.objects.select_related(
        "request", "requested_by", "request__selected_trainer"
    ).get(pk=token_pk)
    req = token.request
    trainer = token.requested_by

    if trainer is None:
        logger.error("undo token %s has no requester; cannot send a code", token_pk)
        return 0

    # The applicant's address is never included. A rejection reversal is the
    # trainer's own correction to make; copying the person into that thread would
    # tell them a rejection is being quietly undone.
    ctx = {
        "request": req,
        "trainer": trainer,
        "code": code,
        "minutes": APPROVAL_UNDO_LIFETIME_MINUTES,
        "queue_url": (
            f"{settings.SITE_URL.rstrip('/')}/accounts/trainer/queue/"
            f"?filter=rejected"
        ),
    }
    return _send(
        f"[Avanyam] Code to reverse the rejection of {req.full_name}",
        strip_tags(render_to_string("accounts/email/undo_code.txt", ctx)),
        render_to_string("accounts/email/undo_code.html", ctx),
        [trainer.email],
    )


@shared_task(name="accounts.notify_applicant_of_reinstatement", **RETRY_KWARGS)
def notify_applicant_of_reinstatement(request_pk: str) -> int:
    """Tell an applicant their rejection was reversed and they are queued again.

    Deliberately says who did it. A person who was told "you are permanently
    declined" and then receives an unexplained "you are in the queue" email has
    been given no way to judge whether the reversal is legitimate.
    """
    from apps.accounts.domain.enums import ApprovalStatus
    from apps.accounts.models import SignupRequest

    req = SignupRequest.objects.select_related("selected_trainer", "decided_by").get(
        pk=request_pk
    )
    if req.status != ApprovalStatus.PENDING:
        # Raced with another decision, or the undo was rolled back after the mail
        # was queued. Saying "you are in the queue" when they are not would be
        # worse than saying nothing.
        logger.warning(
            "request %s is %s, not pending; not sending a reinstatement notice",
            request_pk,
            req.status,
        )
        return 0

    ctx = {
        "request": req,
        "trainer": req.decided_by or req.selected_trainer,
        "queue_url": f"{settings.SITE_URL.rstrip('/')}/accounts/login/",
    }
    return _send(
        f"[Avanyam] Your rejection was reversed -- you are back in the queue",
        strip_tags(render_to_string("accounts/email/applicant_reinstatement.txt", ctx)),
        render_to_string("accounts/email/applicant_reinstatement.html", ctx),
        [req.email],
    )


@shared_task(name="accounts.notify_user_of_password_change", **RETRY_KWARGS)
def notify_user_of_password_change(user_pk: int, via_first_run: bool) -> int:
    """Tell someone their password just changed.

    This is the one notification in the system aimed at protecting an account
    rather than informing about course business, so it is worded as a security
    notice and never contains the password, a reset link, or the old value.

    `via_first_run` distinguishes the seeded-trainee bootstrap change from an
    ordinary change by an established user; the email says which, because "you
    have been asked to set a new password" and "you just changed your password"
    call for different reactions if they did not expect it.
    """
    from apps.accounts.models import User

    user = User.objects.get(pk=user_pk)
    login_url = f"{settings.SITE_URL.rstrip('/')}/accounts/login/"

    # A locked or deactivated account getting a password notice is noise, and
    # mail to an address we no longer consider usable invites a support thread.
    if not user.is_active or not user.email:
        logger.warning("user %s is inactive or has no email; no password notice sent", user_pk)
        return 0

    ctx = {
        "user": user,
        "via_first_run": via_first_run,
        "login_url": login_url,
        "changed_at": timezone.now(),
    }
    subject = (
        "[Avanyam] Set your new password"
        if via_first_run
        else "[Avanyam] Your password was changed"
    )
    return _send(
        subject,
        strip_tags(render_to_string("accounts/email/password_changed.txt", ctx)),
        render_to_string("accounts/email/password_changed.html", ctx),
        [user.email],
    )

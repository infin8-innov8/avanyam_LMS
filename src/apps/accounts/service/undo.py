"""Reversing a rejection.

A rejection is the strongest thing the approval queue does to a person: it blocks
the address from ever applying again, and it is visible to every admin who looks.
`rejected` was originally terminal, and the only fix was a database edit by someone
with shell access -- which is not a fix an admin can perform at 6pm on a Friday when
they realise they rejected the wrong person for the wrong reason.

So the move is permitted, and gated. `request_undo_code()` mails a single-use code
to the actor's own address; `undo_rejection()` redeems it. Between them:

* the code is never stored in plaintext (HMAC under SECRET_KEY), so a database
  disclosure does not yield usable codes;
* it expires in :data:`APPROVAL_UNDO_LIFETIME`;
* it is consumed on first success and cannot be replayed;
* it tolerates :data:`APPROVAL_UNDO_MAX_ATTEMPTS` wrong guesses, then dies;
* requesting a new code invalidates any earlier one for the same request, so an
  old code in an old inbox stops working.

**What this control is worth, honestly.** The code goes to the admin who is
already logged in and already authorised to make the decision, so it proves
control of that mailbox rather than authorising a new actor. It is step-up
authentication, not a second approver: it stops a walk-up browser session from
reversing someone's permanent rejection, and it leaves an audit trail tying the
reversal to a specific mailbox at a specific time. It does **not** stop a
determined admin who also owns the inbox. A genuinely stronger control would
send the code to the *applicant*, making revival a two-party act -- that is a
product decision, and it is the one thing here worth reconsidering.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import NoReturn

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.accounts.domain.enums import ApprovalStatus
from apps.accounts.models import (
    APPROVAL_UNDO_LIFETIME,
    APPROVAL_UNDO_MAX_ATTEMPTS,
    ApprovalUndoToken,
    SignupRequest,
    User,
)
from apps.accounts.policies import can_view_request
from apps.common.logging import get_logger

logger = get_logger(__name__)

#: Six digits, so ten thousand possibilities per prefix. Short enough to retype
#: from an email on a phone, which is where a actor will actually read it.
CODE_LENGTH = 6


class UndoError(Exception):
    """The undo was refused. `message` is safe to show the actor."""


@dataclass(frozen=True, slots=True)
class IssuedCode:
    """The outcome of asking for a code.

    Carries the plaintext so the caller can pass it straight to the mail task,
    and so tests can redeem it without reading the database. It is never logged.
    """

    token: ApprovalUndoToken
    code: str


@dataclass(frozen=True, slots=True)
class UndoOutcome:
    request: SignupRequest
    user: User | None
    token: ApprovalUndoToken


# ---------------------------------------------------------------------------
# Code generation and comparison
# ---------------------------------------------------------------------------


def _hash_code(request_pk, code: str) -> str:
    """HMAC the code against the request and SECRET_KEY.

    Binding the hash to `request_pk` means a code minted for one rejection cannot
    be redeemed against another, even by someone who can read the table.

    `compare_digest` is used at the point of comparison; a plain `==` on a hash
    leaks its length-prefix behaviour through timing, which is a needless gift on
    a value an attacker gets unlimited guesses at.
    """
    message = f"{request_pk}:{code}".encode()
    return hmac.new(
        settings.SECRET_KEY.encode(), message, hashlib.sha256
    ).hexdigest()


def _generate_code() -> str:
    # randbelow, not uniform(0, 10**6): the latter can return short values, and a
    # code that renders as "12345" is one an attacker tries first.
    return f"{secrets.randbelow(10**CODE_LENGTH):0{CODE_LENGTH}d}"


# ---------------------------------------------------------------------------
# Requesting a code
# ---------------------------------------------------------------------------


def request_undo_code(*, actor: User, request_pk) -> IssuedCode:
    """Mint a code for a rejected application and mail it to the actor."""
    with transaction.atomic():
        # Lock bare, then re-read with the joins. See the note in
        # `service.approval.decide`: `user` and `decided_by` are nullable FKs, so
        # select_related() builds an outer join and PostgreSQL refuses
        # `FOR UPDATE` on its nullable side. The row two admins could race over is
        # SignupRequest, so that is the row locked here.
        req = SignupRequest.objects.select_for_update().get(pk=request_pk)
        req = (
            SignupRequest.objects.select_related("user", "decided_by")
            .get(pk=req.pk)
        )

        _assert_may_undo(actor, req)

        # Supersede anything already outstanding. Without this, a actor who
        # asked for a code, ignored it, and asked again would leave the first one
        # live -- and an old code sitting in an old inbox would still work.
        now = timezone.now()
        ApprovalUndoToken.objects.filter(
            request=req, consumed_at__isnull=True
        ).update(expires_at=now)

        code = _generate_code()
        token = ApprovalUndoToken.objects.create(
            request=req,
            requested_by=actor,
            code_hash=_hash_code(req.pk, code),
            expires_at=now + APPROVAL_UNDO_LIFETIME,
        )

    # Outside the transaction on purpose. Sending holds the request row's lock for
    # as long as the SMTP round trip takes, and a row lock spanning network I/O is
    # a good way to turn a slow mail server into a stalled queue. The cost is that
    # the token is already committed by the time we find out the send failed, so
    # the failure is compensated rather than rolled back.
    try:
        _notify_requester_of_code(token.pk, code)
    except UndoError:
        # Expire what we just minted instead of deleting it: the row is the record
        # that a code was asked for, and an expired one cannot be attempted against.
        # A actor whose mail did arrive after a timeout can simply ask again.
        ApprovalUndoToken.objects.filter(
            pk=token.pk, consumed_at__isnull=True
        ).update(expires_at=timezone.now())
        logger.warning(
            "undo.code_expired_after_failed_send",
            "Minted code expired because its send failed",
            outcome="compensated",
            token_pk=str(token.pk),
            request_pk=str(req.pk),
        )
        raise

    logger.info(
        "undo.code_requested",
        "New undo code minted for a rejected application",
        outcome="success",
        token_pk=str(token.pk),
        request_pk=str(req.pk),
    )
    return IssuedCode(token=token, code=code)


def _assert_may_undo(actor: User | None, req: SignupRequest) -> None:
    """Shared gate for both halves of the undo.

    Every refusal is logged with ``reason``, because these are the interesting
    failures: they all mean somebody with a session tried to reverse a decision
    they should not have been able to touch. The user-facing message stays vague
    on purpose -- it is what a actor reads -- but the log says precisely which
    rule stopped them.
    """

    def refuse(message: str, reason: str) -> NoReturn:
        logger.warning(
            "undo.refused",
            "Undo is not available on this application",
            outcome="refused",
            request_pk=str(req.pk),
            actor_pk=str(actor.pk) if actor is not None else None,
            reason=reason,
        )
        raise UndoError(message)

    if actor is None or not actor.is_authenticated:
        refuse("You must be signed in.", "not_authenticated")

    verdict = can_view_request(actor, req)
    if not verdict.allowed:
        refuse(verdict.reason, "not_authorised")

    # `can_view_request` deliberately omits the self-approval rule, because a
    # actor must be able to *read* a refusal naming themselves. Reversing one is
    # not reading: it puts a live account back on an admin's decision. `can_approve`
    # refuses this case first, before even the admin override, so the undo does
    # too rather than relying on a later approval step to catch it.
    if req.user_id is not None and req.user_id == actor.pk:
        refuse(
            "You cannot undo a decision on your own application.",
            "own_application",
        )

    if req.status != ApprovalStatus.REJECTED:
        if req.status == ApprovalStatus.REDIRECTED:
            refuse(
                "This application was declined but the applicant was allowed to "
                "apply again, so there is nothing to undo. They can register "
                "whenever they are ready.",
                "redirected_not_rejected",
            )
        refuse(
            f"This application is {req.status}, not rejected, so there is nothing to undo.",
            "not_rejected",
        )

    # Undo restores the *account*, not just the row. Without a linked user there
    # is nothing to restore, and returning the row to pending would only produce
    # a queue entry that can never be approved.
    if req.user_id is None:
        raise UndoError(
            "This application has no linked account, so it cannot be returned to "
            "the queue. It has to be recreated as a new application."
        )


# ---------------------------------------------------------------------------
# Redeeming a code
# ---------------------------------------------------------------------------


def undo_rejection(*, actor: User, request_pk, code: str) -> UndoOutcome:
    """Return a rejected application to the pending queue.

    The code is consumed and the status moved in one transaction under a row lock,
    so two admins racing with the same valid code produce one revival and one
    refusal rather than two.
    """
    code = (code or "").strip()

    # Set when a code is refused, raised *after* the transaction block. A wrong
    # guess has to be counted durably, and raising from inside `atomic` would
    # roll the count back with everything else -- leaving `attempts` pinned at 0
    # and the guess limit permanently inert.
    refusal: UndoError | None = None
    redeemed_user: User | None = None

    with transaction.atomic():
        # Lock bare, then re-read with the joins. See the note in
        # `service.approval.decide`: `user` and `decided_by` are nullable FKs, so
        # select_related() builds an outer join and PostgreSQL refuses
        # `FOR UPDATE` on its nullable side. The row two admins could race over is
        # SignupRequest, so that is the row locked here.
        req = SignupRequest.objects.select_for_update().get(pk=request_pk)
        req = (
            SignupRequest.objects.select_related("user", "decided_by")
            .get(pk=req.pk)
        )
        _assert_may_undo(actor, req)

        token = (
            ApprovalUndoToken.objects.select_for_update()
            .filter(request=req, consumed_at__isnull=True)
            .order_by("-created_at")
            .first()
        )
        # Each of these refuses for a different reason and the actor is told only
        # that they need a new code, so the log is the only place the distinction
        # survives. A climb in `expired` means mails are not landing; a climb in
        # `wrong_code` means something is guessing.
        if token is None:
            logger.warning(
                "undo.redeem_refused",
                "No live code exists for this application",
                outcome="refused",
                request_pk=str(req.pk),
                token_pk=None,
                reason="no_token",
            )
            raise UndoError(
                "No code is waiting for this application. Request a new one."
            )

        if token.attempts >= APPROVAL_UNDO_MAX_ATTEMPTS:
            logger.warning(
                "undo.redeem_refused",
                "Code was refused because the attempt limit was already reached",
                outcome="refused",
                request_pk=str(req.pk),
                token_pk=str(token.pk),
                reason="attempts_exhausted",
                attempts=token.attempts,
            )
            raise UndoError(
                "Too many incorrect codes. Request a new one to continue."
            )

        if token.expires_at <= timezone.now():
            logger.warning(
                "undo.redeem_refused",
                "Code was refused because it had already expired",
                outcome="refused",
                request_pk=str(req.pk),
                token_pk=str(token.pk),
                reason="expired",
            )
            raise UndoError("This code has expired. Request a new one.")

        if not hmac.compare_digest(token.code_hash, _hash_code(req.pk, code)):
            token.attempts += 1
            token.save(update_fields=["attempts", "updated_at"])
            left = APPROVAL_UNDO_MAX_ATTEMPTS - token.attempts
            # Wording matters here: this is the message a actor reads while
            # staring at a six-digit box, so it says what was wrong with the
            # input rather than judging the attempt. "That code is not right"
            # read as a verdict on the person.
            # The code itself is never recorded, only that the guess was wrong.
            logger.warning(
                "undo.redeem_refused",
                "Code did not match",
                outcome="refused",
                request_pk=str(req.pk),
                token_pk=str(token.pk),
                reason="wrong_code",
                attempts=token.attempts,
                attempts_left=max(left, 0),
            )
            refusal = UndoError(
                "Incorrect code. Try again."
                if left > 1
                else "Incorrect code. No attempts left -- request a new one."
            )
        else:
            redeemed_user = _redeem(req, token, actor)

    if refusal is not None:
        raise refusal

    _notify_applicant_of_revival(req.pk)
    logger.info(
        "undo.code_redeemed",
        "Undo code accepted and the application returned to the queue",
        outcome="success",
        request_pk=str(req.pk),
        token_pk=str(token.pk),
        user_pk=str(redeemed_user.pk) if redeemed_user is not None else None,
    )
    return UndoOutcome(request=req, user=redeemed_user, token=token)


def _redeem(req: SignupRequest, token: ApprovalUndoToken, actor: User) -> User | None:
    """Consume the code and restore the application. Runs inside the lock."""
    token.consumed_at = timezone.now()
    token.save(update_fields=["consumed_at", "updated_at"])

    # rejected -> pending. The refusal that got them here stays on the row:
    # `decision_note` and `decided_by` are left alone, because the next
    # approve overwrites `decision_note` but the audit trail for *this*
    # reversal is the consumed token, not the status field.
    req.transition(ApprovalStatus.PENDING, by=actor)
    req.decided_at = None
    req.save(update_fields=["decided_at", "updated_at"])

    user = req.user
    if user is not None:
        user.move_to(ApprovalStatus.PENDING, by=actor)
    return user


def _notify_requester_of_code(token_pk, code: str) -> None:
    """Send the code now, in this request, and fail loudly if it did not go.

    Synchronous on purpose. The dialog tells the actor a code is on its way, so
    this call has to be able to say no; a queued task could not, and the interface
    would be left claiming delivery for a message that a worker might never run.
    Raising here rolls the token back with everything else, so a failed send leaves
    the application rejected and no half-open code behind.

    An SMTP failure is a server-side fault, so it is logged in full and reported to
    the actor as a plain "we could not send it" with the cause kept server-side.
    """
    from apps.accounts.tasks import deliver_undo_code

    started = time.perf_counter()
    try:
        sent = deliver_undo_code(str(token_pk), code)
    except Exception as exc:
        # Never logged with the code, its hash, or the applicant's address: in this
        # one flow a log line would itself be a credential.
        logger.exception(
            "undo.code_send_failed",
            "The undo code could not be handed to the mail server",
            outcome="failure",
            token_pk=str(token_pk),
        )
        raise UndoError(
            "We could not send the code. Please try again in a moment."
        ) from exc

    if not sent:
        # No exception, but the server accepted zero messages. The actor's
        # experience is the same as an outright failure, so it is logged the same.
        logger.error(
            "undo.code_send_failed",
            "The mail server accepted the message without sending it",
            outcome="failure",
            token_pk=str(token_pk),
            reason="zero_messages_accepted",
        )
        raise UndoError("We could not send the code. Please try again in a moment.")

    logger.info(
        "undo.code_sent",
        "Undo code handed to the mail server",
        outcome="success",
        token_pk=str(token_pk),
        duration_ms=round((time.perf_counter() - started) * 1000, 3),
    )


def _notify_applicant_of_revival(request_pk) -> None:
    from apps.accounts.tasks import notify_applicant_of_reinstatement

    notify_applicant_of_reinstatement.delay(str(request_pk))

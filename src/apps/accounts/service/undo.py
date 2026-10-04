"""Reversing a rejection.

A rejection is the strongest thing the approval queue does to a person: it blocks
the address from ever applying again, and it is visible to every trainer who
looks. `rejected` was originally terminal, and the only fix was a database edit by
someone with shell access -- which is not a fix a trainer can perform at 6pm on a
Friday when they realise they rejected the wrong person for the wrong reason.

So the move is permitted, and gated. `request_undo_code()` mails a single-use code
to the trainer's own address; `undo_rejection()` redeems it. Between them:

* the code is never stored in plaintext (HMAC under SECRET_KEY), so a database
  disclosure does not yield usable codes;
* it expires in :data:`APPROVAL_UNDO_LIFETIME`;
* it is consumed on first success and cannot be replayed;
* it tolerates :data:`APPROVAL_UNDO_MAX_ATTEMPTS` wrong guesses, then dies;
* requesting a new code invalidates any earlier one for the same request, so an
  old code in an old inbox stops working.

**What this control is worth, honestly.** The code goes to the trainer who is
already logged in and already authorised to make the decision, so it proves
control of that mailbox rather than authorising a new actor. It is step-up
authentication, not a second approver: it stops a walk-up browser session from
reversing someone's permanent rejection, and it leaves an audit trail tying the
reversal to a specific mailbox at a specific time. It does **not** stop a
determined trainer who also owns the inbox. A genuinely stronger control would
send the code to the *applicant*, making revival a two-party act -- that is a
product decision, and it is the one thing here worth reconsidering.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

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

#: Six digits, so ten thousand possibilities per prefix. Short enough to retype
#: from an email on a phone, which is where a trainer will actually read it.
CODE_LENGTH = 6


class UndoError(Exception):
    """The undo was refused. `message` is safe to show the trainer."""


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


def request_undo_code(*, trainer: User, request_pk) -> IssuedCode:
    """Mint a code for a rejected application and mail it to the trainer."""
    with transaction.atomic():
        # Lock bare, then re-read with the joins. See the note in
        # `service.approval.decide`: user / selected_trainer / decided_by are all
        # nullable, so select_related() builds an outer join and PostgreSQL
        # refuses `FOR UPDATE` on its nullable side. The row that two trainers
        # could race over is SignupRequest, so that is the row locked here.
        req = SignupRequest.objects.select_for_update().get(pk=request_pk)
        req = (
            SignupRequest.objects.select_related("selected_trainer", "user", "decided_by")
            .get(pk=req.pk)
        )

        _assert_may_undo(trainer, req)

        # Supersede anything already outstanding. Without this, a trainer who
        # asked for a code, ignored it, and asked again would leave the first one
        # live -- and an old code sitting in an old inbox would still work.
        now = timezone.now()
        ApprovalUndoToken.objects.filter(
            request=req, consumed_at__isnull=True
        ).update(expires_at=now)

        code = _generate_code()
        token = ApprovalUndoToken.objects.create(
            request=req,
            requested_by=trainer,
            code_hash=_hash_code(req.pk, code),
            expires_at=now + APPROVAL_UNDO_LIFETIME,
        )

    _notify_trainer_of_code(token.pk, code)
    return IssuedCode(token=token, code=code)


def _assert_may_undo(trainer: User | None, req: SignupRequest) -> None:
    """Shared gate for both halves of the undo."""
    if trainer is None or not trainer.is_authenticated:
        raise UndoError("You must be signed in.")

    verdict = can_view_request(trainer, req)
    if not verdict.allowed:
        raise UndoError(verdict.reason)

    # `can_view_request` deliberately omits the self-approval rule, because a
    # trainer must be able to *read* a refusal naming themselves. Reversing one is
    # not reading: it puts a live account back on an admin's decision. `can_approve`
    # refuses this case first, before even the admin override, so the undo does
    # too rather than relying on a later approval step to catch it.
    if req.user_id is not None and req.user_id == trainer.pk:
        raise UndoError("You cannot undo a decision on your own application.")

    if req.status != ApprovalStatus.REJECTED:
        if req.status == ApprovalStatus.REDIRECTED:
            raise UndoError(
                "This application was declined but the applicant was allowed to "
                "apply again, so there is nothing to undo. They can register "
                "whenever they are ready."
            )
        raise UndoError(
            f"This application is {req.status}, not rejected, so there is nothing to undo."
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


def undo_rejection(*, trainer: User, request_pk, code: str) -> UndoOutcome:
    """Return a rejected application to the pending queue.

    The code is consumed and the status moved in one transaction under a row lock,
    so two trainers racing with the same valid code produce one revival and one
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
        # `service.approval.decide`: user / selected_trainer / decided_by are all
        # nullable, so select_related() builds an outer join and PostgreSQL
        # refuses `FOR UPDATE` on its nullable side. The row that two trainers
        # could race over is SignupRequest, so that is the row locked here.
        req = SignupRequest.objects.select_for_update().get(pk=request_pk)
        req = (
            SignupRequest.objects.select_related("selected_trainer", "user", "decided_by")
            .get(pk=req.pk)
        )
        _assert_may_undo(trainer, req)

        token = (
            ApprovalUndoToken.objects.select_for_update()
            .filter(request=req, consumed_at__isnull=True)
            .order_by("-created_at")
            .first()
        )
        if token is None:
            raise UndoError(
                "There is no code waiting for this application. Ask for a new one."
            )

        if token.attempts >= APPROVAL_UNDO_MAX_ATTEMPTS:
            raise UndoError(
                "Too many wrong codes. Ask for a new one to continue."
            )

        if token.expires_at <= timezone.now():
            raise UndoError("That code has expired. Ask for a new one.")

        if not hmac.compare_digest(token.code_hash, _hash_code(req.pk, code)):
            token.attempts += 1
            token.save(update_fields=["attempts", "updated_at"])
            left = APPROVAL_UNDO_MAX_ATTEMPTS - token.attempts
            refusal = UndoError(
                "That code is not right."
                if left > 1
                else "That code is not right. No attempts left -- ask for a new one."
            )
        else:
            redeemed_user = _redeem(req, token, trainer)

    if refusal is not None:
        raise refusal

    _notify_applicant_of_revival(req.pk)
    return UndoOutcome(request=req, user=redeemed_user, token=token)


def _redeem(req: SignupRequest, token: ApprovalUndoToken, trainer: User) -> User | None:
    """Consume the code and restore the application. Runs inside the lock."""
    token.consumed_at = timezone.now()
    token.save(update_fields=["consumed_at", "updated_at"])

    # rejected -> pending. The refusal that got them here stays on the row:
    # `decision_note` and `decided_by` are left alone, because the next
    # approve overwrites `decision_note` but the audit trail for *this*
    # reversal is the consumed token, not the status field.
    req.transition(ApprovalStatus.PENDING, by=trainer)
    req.decided_at = None
    req.save(update_fields=["decided_at", "updated_at"])

    user = req.user
    if user is not None:
        user.move_to(ApprovalStatus.PENDING, by=trainer)
    return user


def _notify_trainer_of_code(token_pk, code: str) -> None:
    from apps.accounts.tasks import notify_trainer_of_undo_code

    notify_trainer_of_undo_code.delay(str(token_pk), code)


def _notify_applicant_of_revival(request_pk) -> None:
    from apps.accounts.tasks import notify_applicant_of_reinstatement

    notify_applicant_of_reinstatement.delay(str(request_pk))

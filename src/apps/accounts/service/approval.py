"""Approval decisions.

Approval is an audited act by a named human (§15), so `decide()` is the only
writer of `SignupRequest.status` and it refuses anything `policies.can_approve`
rejects. It also promotes the pending User in the same transaction, so we never
hold an approved SignupRequest next to a pending account.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from django.db import transaction

from apps.accounts.domain.enums import ApprovalStatus
from apps.accounts.models import SignupRequest, User
from apps.accounts.policies import can_approve


class DecisionError(Exception):
    """A decision was refused. `message` is safe to show the approver."""


class Decision(str, Enum):
    """The three things a trainer can do to a pending application.

    A plain bool cannot express the third case. `approve: bool` forced "not
    approve" to mean one thing, so "decline, but let them apply again" was
    unreachable and a trainee who picked the wrong trainer had no way back.

    Names are the values posted by the form, so a typo in a template raises
    instead of silently becoming a rejection.
    """

    APPROVE = "approve"
    REJECT = "reject"
    REDIRECT = "redirect"


@dataclass(frozen=True, slots=True)
class DecisionResult:
    request: SignupRequest
    user: User


def decide(
    *,
    approver: User,
    request_pk,
    approve: bool | None = None,
    decision: Decision | str | None = None,
    note: str = "",
) -> DecisionResult:
    """Approve, reject or redirect a signup application.

    `decision` is the real parameter. `approve` is kept as a compatibility shim
    for the existing callers and tests: `approve=True` means APPROVE and
    anything falsy means REJECT, which is exactly what the old two-button form
    meant. New code should pass `decision`.
    """
    if decision is None:
        decision = Decision.APPROVE if approve else Decision.REJECT
    else:
        try:
            decision = Decision(decision)
        except ValueError:
            raise DecisionError("That is not a decision we recognise.") from None

    with transaction.atomic():
        # select_for_update() must NOT be combined with select_related() here.
        # SignupRequest.user / selected_trainer are nullable FKs, so the join is
        # an outer join and PostgreSQL refuses to lock the nullable side:
        #   FOR UPDATE cannot be applied to the nullable side of an outer join
        # The row lock we need is on SignupRequest itself, so fetch it bare and
        # pull the related rows separately. The lock is still correct: it is the
        # pending request that two trainers could race to decide.
        req = SignupRequest.objects.select_for_update().get(pk=request_pk)
        req = (
            SignupRequest.objects.select_related("user", "selected_trainer", "decided_by")
            .get(pk=req.pk)
        )

        verdict = can_approve(approver, req)
        if not verdict.allowed:
            raise DecisionError(verdict.reason)

        if req.status != ApprovalStatus.PENDING:
            # A rejection is reversible, but only through undo_rejection() with a
            # mailed code. Landing here with a decided request means someone tried
            # to short-circuit that, and the state machine would refuse anyway.
            raise DecisionError(
                f"This application was already {req.status} and cannot be changed."
            )

        if decision is Decision.APPROVE:
            req.transition(ApprovalStatus.APPROVED, by=approver)
            req.decision_note = note
            req.save(update_fields=["decision_note", "updated_at"])

            user = req.user
            if user is None:
                raise DecisionError("This application is not linked to an account.")
            user.move_to(ApprovalStatus.APPROVED, by=approver)

            # Deliberately NO role grant here.
            #
            # Roles are assigned where the person is created, never at approval:
            #   * submit_application()  -> trainee   (service/signup.py)
            #   * seed_people           -> trainer   (management command)
            #
            # An earlier version branched on `req.selected_trainer_id ==
            # approver.pk` and granted ROLE_TRAINER. That condition is true for
            # EVERY normal application, because the approver is by definition the
            # trainer the applicant selected. The effect was that approving one
            # ordinary trainee promoted them to trainer -- they could then open
            # the approval queue and approve other people. Verified, then removed.
            #
            # If trainer onboarding is ever needed, it needs its own request type
            # carrying the requested role plus a second, independent approver.
            # Reusing this flow would re-open the same hole.
        else:
            target = (
                ApprovalStatus.REJECTED
                if decision is Decision.REJECT
                else ApprovalStatus.REDIRECTED
            )
            req.transition(target, by=approver)
            req.decision_note = note
            req.save(update_fields=["decision_note", "updated_at"])

            if req.user_id is not None:
                user = req.user
                if decision is Decision.REDIRECT:
                    # Remove the stub account. The person never had access -- a
                    # pending user is inert -- so nothing is lost, and leaving it
                    # in place would block re-registration twice over: once by
                    # `User.objects.filter(email=...)` in submit_application, and
                    # again by SignupRequest.user being OneToOne, so the revived
                    # address could never be linked to a second application.
                    #
                    # The SignupRequest survives with user=NULL as the durable
                    # record of the redirect, so `user` is reported as None.
                    user.delete()
                    user = None
                else:
                    user.move_to(target, by=approver)
            else:
                user = None

        req.refresh_from_db()

    _notify_applicant(req.pk)
    return DecisionResult(request=req, user=user)


def _notify_applicant(request_pk) -> None:
    from apps.accounts.tasks import notify_applicant_of_decision

    notify_applicant_of_decision.delay(str(request_pk))

"""Approval decisions.

Approval is an audited act by a named human (§15), so `decide()` is the only
writer of `SignupRequest.status` and it refuses anything `policies.can_approve`
rejects. It also promotes the pending User in the same transaction, so we never
hold an approved SignupRequest next to a pending account.

**Approval is where the role is granted.** Signup records what was asked for and
grants nothing (`service/signup.py`); an Admin may substitute a different role
from the same vocabulary, and the granted role is written here in the same
transaction as the approval so the two cannot come apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from django.db import transaction

from apps.accounts.domain.enums import (
    REQUESTABLE_ROLES,
    ApprovalStatus,
    RequestedRole,
)
from apps.accounts.models import Role, RoleAssignment, SignupRequest, User
from apps.accounts.policies import can_approve
from apps.common.logging import get_logger

logger = get_logger(__name__)


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
    #: The role actually granted. Differs from `request.requested_role` when the
    #: Admin overrode it, which is the case the applicant is told about.
    granted_role: str | None = None


def _resolve_grant_role(req: SignupRequest, override: str | None) -> str:
    """The role to grant: the Admin's override, or what was requested.

    Both come from `RequestedRole`, which has no `ADMIN` member, so an approval
    can never mint an admin (D44). The validator is a second refusal on the same
    path the form validates, because `decide()` is called from tests, the queue
    and anything else that reaches the service directly.
    """
    raw = override if override not in (None, "") else req.requested_role
    try:
        role = RequestedRole(raw)
    except ValueError as exc:
        raise DecisionError("That is not a role you can grant.") from exc
    if role not in REQUESTABLE_ROLES:
        raise DecisionError("That is not a role you can grant.")
    return role.value


def decide(
    *,
    approver: User,
    request_pk,
    approve: bool | None = None,
    decision: Decision | str | None = None,
    note: str = "",
    role: str | None = None,
) -> DecisionResult:
    """Approve, reject or redirect a signup application.

    `decision` is the real parameter. `approve` is kept as a compatibility shim
    for the existing callers and tests: `approve=True` means APPROVE and
    anything falsy means REJECT, which is exactly what the old two-button form
    meant. New code should pass `decision`.

    `role` is the Admin's override. Empty means "grant what the applicant asked
    for"; a value from `RequestedRole` means "grant this instead", and the
    applicant is told in the decision email.
    """
    if decision is None:
        decision = Decision.APPROVE if approve else Decision.REJECT
    else:
        try:
            decision = Decision(decision)
        except ValueError as exc:
            # Logged because it cannot come from our own form: the value
            # arrived in a request, so either a stale client or a direct POST.
            logger.warning(
                "approval.decision_unrecognised",
                "A decision value was posted that this build does not know",
                outcome="refused",
                request_pk=str(request_pk),
                decision=decision,
            )
            raise DecisionError("That is not a decision we recognise.") from exc

    with transaction.atomic():
        # select_for_update() must NOT be combined with select_related() here.
        # SignupRequest.user / selected_trainer are nullable FKs, so the join is
        # an outer join and PostgreSQL refuses to lock the nullable side:
        #   FOR UPDATE cannot be applied to the nullable side of an outer join
        # The row lock we need is on SignupRequest itself, so fetch it bare and
        # pull the related rows separately. The lock is still correct: it is the
        # pending request that two admins could race to decide.
        req = SignupRequest.objects.select_for_update().get(pk=request_pk)
        req = (
            SignupRequest.objects.select_related("user", "selected_trainer", "decided_by")
            .get(pk=req.pk)
        )

        verdict = can_approve(approver, req)
        if not verdict.allowed:
            # Worth a record of its own: this is the shape an authorisation
            # attempt takes, and the one you want to see climbing.
            logger.warning(
                "approval.refused",
                "Approver is not permitted to decide this application",
                outcome="refused",
                request_pk=str(request_pk),
                approver_pk=str(approver.pk),
                reason=verdict.reason,
            )
            raise DecisionError(verdict.reason)

        if req.status != ApprovalStatus.PENDING:
            # A rejection is reversible, but only through undo_rejection() with a
            # mailed code. Landing here with a decided request means someone tried
            # to short-circuit that, and the state machine would refuse anyway.
            logger.warning(
                "approval.refused",
                "Application was already decided and cannot be changed again",
                outcome="refused",
                request_pk=str(request_pk),
                approver_pk=str(approver.pk),
                status=req.status,
                attempted=decision.value,
            )
            raise DecisionError(
                f"This application was already {req.status} and cannot be changed."
            )

        granted_role: str | None = None

        if decision is Decision.APPROVE:
            granted_role = _resolve_grant_role(req, role)
            req.transition(ApprovalStatus.APPROVED, by=approver)
            req.decision_note = note
            req.save(update_fields=["decision_note", "updated_at"])

            user = req.user
            if user is None:
                raise DecisionError("This application is not linked to an account.")
            user.move_to(ApprovalStatus.APPROVED, by=approver)

            # The role is granted here and nowhere else. `rules.md` §6: never
            # grant a role without an approval decision -- signup stores the
            # request and grants nothing, so a rejected applicant's account never
            # held a role.
            #
            # `get_or_create` rather than `create`: the unique constraint on
            # (user, role) is real, and an admin who approves twice through a
            # retried POST would otherwise raise IntegrityError on an account
            # they had just successfully approved.
            role_row, _ = Role.objects.get_or_create(
                slug=granted_role,
                defaults={"name": granted_role.capitalize()},
            )
            RoleAssignment.objects.get_or_create(
                user=user, role=role_row, defaults={"assigned_by": approver}
            )
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

    logger.info(
        "approval.decided",
        "Application decided",
        outcome="success",
        request_pk=str(req.pk),
        decision=decision.value,
        status=req.status,
        approver_pk=str(approver.pk),
        user_pk=str(user.pk) if user is not None else None,
        requested_role=req.requested_role,
        granted_role=granted_role,
        role_overridden=bool(granted_role and granted_role != req.requested_role),
    )
    _notify_applicant(req.pk)
    return DecisionResult(request=req, user=user, granted_role=granted_role)


def _notify_applicant(request_pk) -> None:
    from apps.accounts.tasks import notify_applicant_of_decision

    notify_applicant_of_decision.delay(str(request_pk))

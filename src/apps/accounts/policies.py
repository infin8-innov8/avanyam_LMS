"""Authorization policies for the accounts context.

`architecture.md` §7: "Authorization never via scattered `if user.is_trainer`
in views." Every view asks a policy function instead, so the matrix is one file
that can be read -- and tested -- in one place.

Two rules here are load-bearing:

* **Separation of duties.** A trainer may not act on their own application.
  Without this, choosing your own approver at signup is a privilege-escalation
  path: pick yourself, approve yourself, gain the trainer role.
* **Only the chosen trainer decides.** §15 makes approval an audited act by a
  named human. If any trainer could approve any trainee, "your trainer approved
  you" would stop meaning anything.
"""

from __future__ import annotations

from dataclasses import dataclass

from apps.accounts.domain.enums import ApprovalStatus
from apps.accounts.models import (
    ROLE_ADMIN,
    ROLE_TRAINER,
    SignupRequest,
    User,
)


@dataclass(frozen=True, slots=True)
class Decision:
    """Why a policy said yes or no. Rendered to the user; never swallowed."""

    allowed: bool
    reason: str = ""


def is_approved(user: User | None) -> bool:
    """The master gate. Inert until approved (§15)."""
    return bool(user and user.is_authenticated and user.is_approved and user.is_active)


def is_superuser(user: User | None) -> bool:
    """Django's own override, honoured here so operators are not locked out.

    `manage.py createsuperuser` sets `is_superuser`/`is_staff` and grants **no**
    RoleAssignment. Every gate below is role-based, so a real operator -- the
    person who can edit users in /admin/ -- was refused the approval queue with
    a bare 404 even though they outrank every trainer. That is the wrong kind of
    strictness: it does not protect the separation-of-duties rule (which is
    enforced separately, below, and still refuses self-approval), it just makes
    the queue unreachable for whoever is on call.

    Placed as its own predicate so the reason is greppable. It is deliberately
    NOT folded into `is_approved`, which is the gate for *trainee-facing*
    capabilities such as registering on behalf of others -- a superuser account
    is not automatically entitled to act as an approved LMS user.
    """
    return bool(user and user.is_authenticated and user.is_superuser and user.is_active)


def can_register(requester: User | None) -> bool:
    """An authenticated, approved person may submit applications on behalf of others."""
    return is_approved(requester)


def can_approve(approver: User | None, request: SignupRequest) -> Decision:
    """May ``approver`` decide ``request``?"""
    if not is_approved(approver):
        return Decision(False, "Your account is not approved.")

    assert approver is not None  # narrowed by is_approved

    # Separation of duties, checked FIRST -- before the admin override, before the
    # superuser override, and before the role check. Nobody decides their own case,
    # including an admin or a superuser: allowing it for them but not trainers
    # would be an inconsistency in exactly the rule §7 depends on.
    if request.user_id is not None and request.user_id == approver.pk:
        return Decision(False, "You cannot decide your own application.")

    # A superuser outranks every role below, so they may clear the backlog --
    # including applications assigned to a trainer, which no admin can touch.
    if is_superuser(approver) or approver.has_role(ROLE_ADMIN):
        return Decision(True)

    if not approver.has_role(ROLE_TRAINER):
        return Decision(False, "Only trainers and admins can decide applications.")

    trainer = request.selected_trainer
    if trainer is None:
        return Decision(
            False,
            "This applicant has not chosen a trainer, so no trainer can approve them. "
            "An admin must handle it.",
        )

    if trainer.pk != approver.pk:
        return Decision(
            False,
            "Only the trainer this applicant selected may approve their application.",
        )

    return Decision(True)


def can_view_queue(approver: User | None) -> bool:
    """Show the approval queue to approved trainers, admins and superusers."""
    if not is_approved(approver):
        return False
    assert approver is not None
    return (
        approver.has_role(ROLE_TRAINER)
        or approver.has_role(ROLE_ADMIN)
        or is_superuser(approver)
    )


def can_view_request(viewer: User | None, request: SignupRequest) -> Decision:
    """May ``viewer`` see this specific application at all?

    Narrower than :func:`can_approve` on one point: it does not apply the
    separation-of-duties rule. A trainer can *see* an application naming
    themselves -- they need to, to read the refusal reason -- but still cannot
    decide it.

    Undoing a rejection needs this rather than `can_approve`, because undo is
    available on a row `can_approve` would refuse outright (it is not pending).
    Reusing the decision policy there would have made reversal impossible for the
    one person entitled to perform it.
    """
    if viewer is None or not viewer.is_authenticated:
        return Decision(False, "You must be signed in.")

    if not is_approved(viewer):
        return Decision(False, "Your account is not approved.")

    if is_superuser(viewer) or viewer.has_role(ROLE_ADMIN):
        return Decision(True)

    if not viewer.has_role(ROLE_TRAINER):
        return Decision(False, "Only trainers and admins can see applications.")

    if request.selected_trainer_id != viewer.pk:
        return Decision(
            False,
            "That application belongs to another trainer's queue.",
        )

    return Decision(True)


def can_undo(request: SignupRequest) -> Decision:
    """Is this row in a state where an undo is even offered?

    Split from :func:`can_view_request` because it depends only on the request,
    not on who is asking -- so the template can hide the button without a policy
    call per row per render.
    """
    if request.status != ApprovalStatus.REJECTED:
        return Decision(False, "Only a rejected application can be undone.")
    if request.user_id is None:
        return Decision(
            False,
            "This application has no linked account, so it cannot be returned to the queue.",
        )
    return Decision(True)


def visible_requests(approver: User) -> list[SignupRequest]:
    """The queue contents. Admins and superusers see everything; a trainer sees only their own."""
    qs = SignupRequest.objects.select_related("selected_trainer")
    if approver.has_role(ROLE_ADMIN) or is_superuser(approver):
        return list(qs)
    return list(qs.filter(selected_trainer=approver))

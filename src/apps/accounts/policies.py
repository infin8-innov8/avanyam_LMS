"""Authorization policies for the accounts context.

`architecture.md` §7: "Authorization never via scattered `if user.is_trainer`
in views." Every view asks a policy function instead, so the matrix is one file
that can be read -- and tested -- in one place.

**Who decides what (D41, 2026-10-04).** Approval is an Admin-only act in a single
queue. The applicant no longer nominates an approver, and the per-trainer split
this file used to encode is gone; `SignupRequest.requested_role` records what was
asked for, and an Admin may override it at approval time. Two rules survive from
the old model and still carry the weight:

* **Separation of duties.** Nobody decides their own case, Admin included. The
  check runs before the Admin override, so the one role that can otherwise do
  anything still cannot approve itself.
* **Approval status gates everything.** An unapproved account is inert, so it
  cannot approve, cannot be in the queue, and cannot grant itself a role.

`selected_trainer` survives in the schema as a deprecated column (D42) and
nothing here reads it. If a predicate starts consulting it again, the queue has
silently become per-trainer again.
"""

from __future__ import annotations

from dataclasses import dataclass

from apps.accounts.domain.enums import ApprovalStatus, RequestedRole
from apps.accounts.models import (
    ROLE_ADMIN,
    ROLE_TRAINEE,
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
    a bare 404 even though they outrank everyone. That is the wrong kind of
    strictness: it does not protect the separation-of-duties rule (which is
    enforced separately, in `can_approve`, and still refuses self-approval), it
    just makes the queue unreachable for whoever is on call.

    Placed as its own predicate so the reason is greppable. It is deliberately
    NOT folded into `is_approved`, which is the gate for *trainee-facing*
    capabilities such as registering on behalf of others -- a superuser account
    is not automatically entitled to act as an approved LMS user.
    """
    return bool(user and user.is_authenticated and user.is_superuser and user.is_active)


def is_admin(user: User | None) -> bool:
    """May this account exercise Admin authority?

    Requires the `admin` Role **and** an approved, active account, so an admin
    row on a suspended or half-created user confers nothing. `is_superuser` is
    accepted as the break-glass override for the same reason it is honoured
    elsewhere.
    """
    if not is_approved(user):
        return False
    assert user is not None  # narrowed by is_approved
    return user.has_role(ROLE_ADMIN) or is_superuser(user)


def is_trainer_or_admin(user: User | None) -> bool:
    """Approved staff who may create accounts for other people."""
    if not is_approved(user):
        return False
    assert user is not None  # narrowed by is_approved
    return user.has_role(ROLE_TRAINER) or user.has_role(ROLE_ADMIN) or is_superuser(user)


def can_register(requester: User | None) -> Decision:
    """May ``requester`` submit an application on behalf of somebody else?

    Trainers and Admins only. A trainee registering other people would be a
    quiet path to manufacturing approved identities, so the check is against
    the *role*, not merely against being signed in and approved.
    """
    if requester is None or not requester.is_authenticated:
        return Decision(False, "You must be signed in to register someone else.")
    if not is_approved(requester):
        return Decision(False, "Your account is not approved.")
    if is_trainer_or_admin(requester):
        return Decision(True)
    return Decision(
        False,
        "Only trainers and admins can register an account for someone else.",
    )


def can_approve(approver: User | None, request: SignupRequest) -> Decision:
    """May ``approver`` decide ``request``? Admins and superusers only."""
    if not is_approved(approver):
        return Decision(False, "Your account is not approved.")

    assert approver is not None  # narrowed by is_approved

    # Separation of duties, checked FIRST -- before the Admin override. Nobody
    # decides their own case, including an admin: allowing it for them but not
    # for anyone else would be an inconsistency in exactly the rule §7 depends on.
    if request.user_id is not None and request.user_id == approver.pk:
        return Decision(False, "You cannot decide your own application.")

    if is_admin(approver):
        return Decision(True)

    return Decision(False, "Only admins can decide applications.")


def can_view_queue(approver: User | None) -> bool:
    """Show the approval queue to Admins and superusers. Nothing else."""
    return is_admin(approver)


def can_view_request(viewer: User | None, request: SignupRequest) -> Decision:
    """May ``viewer`` see this specific application at all?

    Narrower than :func:`can_approve` on one point: it does not apply the
    separation-of-duties rule. An Admin can *see* an application naming
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

    if is_admin(viewer):
        return Decision(True)

    return Decision(False, "Only admins can see applications.")


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


def can_manage_roles(actor: User | None) -> Decision:
    """May ``actor`` grant or revoke roles on other accounts?

    Admins only, and it is a *live* grant rather than an approval step: an Admin
    may promote an approved Trainer or demote one back to Trainee (D41) without
    a new signup request, because the person is already known.

    Self-management is refused here rather than in the service, so the reason is
    visible in the authorization matrix and not just in a transaction. Removing
    your own last admin role is how a deployment ends up with nobody who can
    approve anybody.
    """
    if actor is None or not actor.is_authenticated:
        return Decision(False, "You must be signed in.")
    if not is_approved(actor):
        return Decision(False, "Your account is not approved.")
    if not is_admin(actor):
        return Decision(False, "Only admins can manage roles.")
    return Decision(True)


def can_assign_role(actor: User | None, subject: User | None, slug: str) -> Decision:
    """May ``actor`` put ``slug`` on ``subject``?

    The two role-specific guards live here, not in the view:

    * the same admin cannot change their own roles, so the last admin cannot be
      demoted away by accident;
    * `admin` is unreachable by role management, so the only way to mint an
      admin stays `manage.py createadmin` (D44). Otherwise this screen would be a
      second, quieter signup path to admin.
    """
    verdict = can_manage_roles(actor)
    if not verdict.allowed:
        return verdict

    if subject is not None and actor is not None and subject.pk == actor.pk:
        return Decision(False, "You cannot change your own roles.")

    if slug == ROLE_ADMIN:
        return Decision(
            False,
            "Admins are created with 'manage.py createadmin', not from here.",
        )
    if slug not in {ROLE_TRAINEE, ROLE_TRAINER}:
        return Decision(False, "That is not a role you can assign.")
    return Decision(True)


def visible_requests(approver: User | None) -> list[SignupRequest]:
    """The queue contents. Admins see every application; nobody else sees any.

    The permission check lives *inside* this function rather than only at the
    view, because the previous version took an `approver` argument, ignored it,
    and returned the whole table. That was only safe because `admin_queue` checked
    `can_view_queue` first -- so the safety of a table containing every applicant
    email in the system rested on one caller remembering to gate. A function
    shaped like this gets reused, and the next caller will not know.

    Returning an empty list rather than raising keeps this usable for building a
    404: the caller can ask "what may they see?" and get the honest answer.
    """
    if not can_view_queue(approver):
        return []
    return list(SignupRequest.objects.all())


#: Roles an applicant may ask for. Re-exported so the signup form, the service
#: validator and this module agree by construction rather than by three literals.
REQUESTABLE = tuple(RequestedRole)
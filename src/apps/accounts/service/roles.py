"""Role management after approval.

An Admin can promote an approved Trainer, or demote one back to Trainee, without
waiting for a new signup request. The person is already known and already
approved, so a second application would be ceremony.

Two boundaries live in `policies.can_assign_role` rather than here, so they show
up in the authorization matrix test rather than only in a transaction:

* an Admin cannot change **their own** roles -- otherwise the last Admin can be
  demoted and nobody is left who can approve anybody (D48);
* `admin` is not an assignable or revocable slug here -- admins are minted only
  by `manage.py createadmin`, so this module is not a second signup path to admin.

`set_role` is the single write path, so "promote" and "demote" cannot drift apart
into two code paths that each implement half the transition.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction

from apps.accounts.models import (
    ROLE_TRAINEE,
    ROLE_TRAINER,
    Role,
    RoleAssignment,
    User,
)
from apps.accounts.policies import can_assign_role
from apps.common.logging import get_logger

logger = get_logger(__name__)

#: The roles this module may move an account between. `admin` is absent on
#: purpose -- it is minted by `manage.py createadmin` and is not a target here.
ASSIGNABLE: frozenset[str] = frozenset({ROLE_TRAINEE, ROLE_TRAINER})


class RoleError(Exception):
    """A role change was refused. `message` is safe to show the admin."""


@dataclass(frozen=True, slots=True)
class RoleOutcome:
    subject: User
    role: str
    granted: bool
    #: The roles this call actually changed. A demotion is a revoke plus a grant,
    #: so this is how the caller reports what happened.
    changed: tuple[str, ...] = ()


def _role_row(slug: str) -> Role:
    row, _ = Role.objects.get_or_create(
        slug=slug, defaults={"name": slug.capitalize()}
    )
    return row


def current_roles(user: User) -> list[str]:
    """The subject's role slugs, sorted. For the role-management screen."""
    return sorted(
        user.role_assignments.select_related("role").values_list("role__slug", flat=True)
    )


def set_role(*, actor: User, subject: User, slug: str) -> RoleOutcome:
    """Make ``subject``'s single role exactly ``slug``.

    A Trainee holds one role, so "set" rather than "add": the previous role is
    revoked in the same transaction. That is what makes the Admin's "demote this
    Trainer back to Trainee" button one action with no intermediate state in
    which the person holds two roles.
    """
    verdict = can_assign_role(actor, subject, slug)
    if not verdict.allowed:
        logger.warning(
            "roles.change_refused",
            "Role change was not permitted",
            outcome="refused",
            actor_pk=str(actor.pk) if actor is not None else None,
            subject_pk=str(subject.pk) if subject is not None else None,
            slug=slug,
            reason=verdict.reason,
        )
        raise RoleError(verdict.reason)

    if subject.approval_status != subject.APPROVED:
        raise RoleError(
            "Only an approved account can be given a role. Approve their "
            "application first."
        )

    with transaction.atomic():
        # Locked so two admins pressing the same button at once cannot interleave
        # a revoke and a grant into a moment where the account holds no role.
        locked = User.objects.select_for_update().get(pk=subject.pk)
        row = _role_row(slug)

        # The last admin is not reachable through here (can_assign_role refuses
        # the slug), so the only roles to clear are the two learner roles. An
        # admin flag on the subject is left alone deliberately.
        removable = RoleAssignment.objects.filter(
            user=locked,
            role__slug__in=ASSIGNABLE,
        ).exclude(role=row)
        cleared = sorted(removable.values_list("role__slug", flat=True))
        removable.delete()

        _, created = RoleAssignment.objects.get_or_create(
            user=locked, role=row, defaults={"assigned_by": actor}
        )
        locked.refresh_from_db(fields=["updated_at"])

    granted = current_roles(locked)
    logger.info(
        "roles.changed",
        "Role set on an account",
        outcome="success",
        actor_pk=str(actor.pk),
        subject_pk=str(subject.pk),
        requested_role=slug,
        roles_after=granted,
        cleared_roles=cleared,
        role_added=created,
    )
    changed = tuple(cleared + ([slug] if created else []))
    return RoleOutcome(subject=locked, role=slug, granted=True, changed=changed)


def revoke_role(*, actor: User, subject: User, slug: str) -> RoleOutcome:
    """Remove ``slug`` from ``subject`` without adding anything.

    Separate from `set_role` because "strip admin rights from someone without
    telling them what they now are" is a real action with no sensible default
    successor role.
    """
    if slug not in ASSIGNABLE:
        raise RoleError(
            "Admins are managed with 'manage.py createadmin', not from here."
        )
    verdict = can_assign_role(actor, subject, slug)
    if not verdict.allowed:
        logger.warning(
            "roles.revoke_refused",
            "Role revocation was not permitted",
            outcome="refused",
            actor_pk=str(actor.pk) if actor is not None else None,
            subject_pk=str(subject.pk) if subject is not None else None,
            slug=slug,
            reason=verdict.reason,
        )
        raise RoleError(verdict.reason)

    with transaction.atomic():
        locked = User.objects.select_for_update().get(pk=subject.pk)
        deleted, _ = RoleAssignment.objects.filter(
            user=locked, role__slug=slug
        ).delete()

    logger.info(
        "roles.revoked",
        "Role removed from an account",
        outcome="success" if deleted else "noop",
        actor_pk=str(actor.pk),
        subject_pk=str(subject.pk),
        slug=slug,
    )
    return RoleOutcome(
        subject=locked, role=slug, granted=False, changed=(slug,) if deleted else ()
    )
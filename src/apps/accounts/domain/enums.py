"""Pure domain vocabulary for the accounts bounded context.

No Django imports here. These are the enums and the approval state machine that
`architecture.md` §6.2 pins down:

    User.approval_status  in {pending, approved, rejected, suspended}
    User.auth_source      in {signup, ldap, oidc, local}

Keeping the transitions here (rather than in a view or a serializer) means the
"signup is inert until approved" invariant has exactly one definition.
"""

from __future__ import annotations

from enum import StrEnum


class AuthSource(StrEnum):
    """How an identity was established. §6.2.

    ``local`` is the break-glass admin path ONLY. It exists for the case where
    the directory and the IdP are both unreachable; it is not a normal way for
    a person to sign in.
    """

    SIGNUP = "signup"
    LDAP = "ldap"
    OIDC = "oidc"
    LOCAL = "local"


class ApprovalStatus(StrEnum):
    """Account lifecycle. §6.2."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUSPENDED = "suspended"

    #: A decline that is *not* a rejection.
    #:
    #: A trainer picks this when the applicant simply chose the wrong trainer --
    #: wrong discipline, wrong region, the wrong person. It ends this application
    #: but leaves the address free to apply again, which is the whole difference
    #: from REJECTED and the reason it is a separate state rather than a flag.
    #:
    #: It applies to a SignupRequest. A User never sits in this state: a redirected
    #: applicant has no usable account, so the pending stub is removed and the
    #: SignupRequest row (user=NULL) is the record.
    REDIRECTED = "redirected"


#: Statuses that may still perform work in the system. Everything else is inert.
ACTIVE_STATUSES = frozenset({ApprovalStatus.APPROVED})

#: Statuses of a SignupRequest that stop the same address applying again.
#:
#: REDIRECTED is deliberately absent: that decline exists to *permit* another
#: application, so it is the one decided state the durable blocklist ignores.
BLOCKING_STATUSES = frozenset(
    {
        ApprovalStatus.PENDING,
        ApprovalStatus.APPROVED,
        ApprovalStatus.REJECTED,
        ApprovalStatus.SUSPENDED,
    }
)

#: Transitions the approval state machine permits, keyed by current status.
#:
#: ``suspended -> approved`` is deliberately absent: reinstating a suspended
#: account is a distinct administrative act with an audit trail, not a
#: side-effect of an approval click.
#:
#: ``rejected -> pending`` was also terminal once. It is now permitted, because a
#: trainer who rejected the wrong person had no way to put it right. It is not
#: reachable by a plain status write: `service.approval.undo_rejection()` is the
#: only caller, it requires a single-use OTP mailed to the trainer, and it leaves
#: the rejection on record. The state machine expresses that the move is legal,
#: not that it is easy -- the same way ``approved -> suspended`` is legal but
#: administrative.
#:
#: ``redirected`` is terminal. The applicant may apply again, but that is a *new*
#: SignupRequest row against a new trainer, not a revival of this one; letting it
#: return to pending would resurrect an application whose whole premise (the
#: chosen trainer) was wrong.
TRANSITIONS: dict[ApprovalStatus, frozenset[ApprovalStatus]] = {
    ApprovalStatus.PENDING: frozenset(
        {
            ApprovalStatus.APPROVED,
            ApprovalStatus.REJECTED,
            ApprovalStatus.REDIRECTED,
        }
    ),
    ApprovalStatus.REJECTED: frozenset({ApprovalStatus.PENDING}),
    ApprovalStatus.REDIRECTED: frozenset(),
    ApprovalStatus.SUSPENDED: frozenset(),
    ApprovalStatus.APPROVED: frozenset({ApprovalStatus.SUSPENDED}),
}


class InvalidTransition(ValueError):
    """Raised when a caller attempts a state change the machine forbids."""


def can_transition(current: ApprovalStatus, target: ApprovalStatus) -> bool:
    return target in TRANSITIONS.get(current, frozenset())


def assert_transition(current: ApprovalStatus, target: ApprovalStatus) -> None:
    if not can_transition(current, target):
        allowed = ", ".join(sorted(TRANSITIONS.get(current, frozenset()))) or "none"
        raise InvalidTransition(
            f"cannot move approval_status from {current!r} to {target!r} "
            f"(allowed from {current!r}: {allowed})"
        )

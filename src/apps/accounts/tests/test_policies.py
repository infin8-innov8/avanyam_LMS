"""The authorization matrix, tested as a matrix.

`architecture.md` §7 forbids scattering `if user.is_trainer` through the views, so
every authorization decision routes through `apps/accounts/policies.py`. That
concentrates the rules in one file -- which means the rules are only as good as
the tests covering that file.

The bug this file exists to prevent is real and was found in this codebase:
`service/approval.py` used to grant the `trainer` role to the approved user, and
gated it on `req.selected_trainer_id == approver.pk` -- a condition true for
*every* ordinary application, since the approver is by definition the trainer the
applicant picked. A plain trainee therefore became a trainer and could then
approve others.

So these tests are deliberately exhaustive about the axes that matter, rather
than one example per rule.
"""

from __future__ import annotations

import itertools

import pytest

from apps.accounts.domain.enums import RequestedRole
from apps.accounts.models import (
    ROLE_ADMIN,
    ROLE_TRAINEE,
    ROLE_TRAINER,
    Role,
    SignupRequest,
    User,
)
from apps.accounts.policies import (
    can_approve,
    can_register,
    can_view_queue,
    is_approved,
    is_superuser,
    visible_requests,
)

from .conftest import make_user

pytestmark = pytest.mark.django_db

#: SignupRequest.email is unique, and most matrix cases need more than one
#: application, so each helper call gets a distinct address.
_applicant_emails = (f"applicant{n}.example.test" for n in itertools.count())


def _application(*, applicant=None, trainer=None, status="pending") -> SignupRequest:
    return SignupRequest.objects.create(
        email=next(_applicant_emails),
        full_name="An Applicant",
        user=applicant,
        selected_trainer=trainer,
        status=status,
    )


# ---------------------------------------------------------------------------
# is_approved -- the master gate
# ---------------------------------------------------------------------------


def test_nobody_is_not_approved() -> None:
    assert is_approved(None) is False


def test_pending_user_is_not_approved(trainee) -> None:
    """`trainee` is created unapproved; approval is what makes a person real."""
    assert trainee.approval_status == "pending"
    assert is_approved(trainee) is False


def test_approved_user_is_approved(trainer) -> None:
    assert is_approved(trainer) is True


def test_deactivated_user_is_not_approved_even_if_approved(trainer) -> None:
    """Approval alone is not enough; a disabled account must stay inert."""
    trainer.is_active = False
    trainer.save(update_fields=["is_active"])
    assert is_approved(trainer) is False


def test_can_register_requires_approval(trainee, trainer) -> None:
    assert can_register(trainer).allowed is True
    assert can_register(trainee).allowed is False
    assert can_register(None).allowed is False


# ---------------------------------------------------------------------------
# can_approve -- the full matrix
# ---------------------------------------------------------------------------


def test_trainer_may_not_approve_anything(trainer) -> None:
    """There is no trainer queue any more (D41), so this is the base case.

    It replaced a test asserting that a trainer could approve the applicant who
    named them. Nothing in the model replaces that authority -- keeping a test in
    the old shape would imply there is a privileged trainer somewhere.
    """
    decision = can_approve(trainer, _application(trainer=trainer))
    assert decision.allowed is False
    assert "Only admins" in decision.reason


def test_one_trainer_is_no_more_privileged_than_another(trainer, other_trainer) -> None:
    """Both trainers are equally unable, which is what a shared queue means.

    The old pair of tests distinguished them: the chosen trainer could decide,
    the other could not. With no chosen trainer, they are the same row in the
    same position.
    """
    req = _application(trainer=trainer)
    assert can_approve(other_trainer, req).allowed is False
    assert can_approve(trainer, req).allowed is False


def test_pending_trainer_may_not_approve(trainer) -> None:
    unapproved = make_user("unapproved.trainer@example.test", role=ROLE_TRAINER, approved=False)
    assert can_approve(unapproved, _application(trainer=trainer)).allowed is False


def test_ordinary_trainee_may_not_approve(trainer) -> None:
    """The regression that started all this."""
    nobody = make_user("nosy.trainee@example.test", role=ROLE_TRAINEE)
    decision = can_approve(nobody, _application(trainer=trainer))
    assert decision.allowed is False
    assert "Only admins" in decision.reason


def test_admin_may_approve_anything(admin, trainer, other_trainer) -> None:
    """Admins decide everything, whoever the application once named. Pinning it."""
    assert can_approve(admin, _application(trainer=trainer)).allowed is True
    assert can_approve(admin, _application(trainer=other_trainer)).allowed is True


def test_pending_admin_may_not_approve(admin) -> None:
    """The admin override is still gated on being approved."""
    unapproved = make_user("unapproved.admin@example.test", role=ROLE_ADMIN, approved=False)
    assert can_approve(unapproved, _application()).allowed is False


def test_admin_can_approve_when_no_trainer_was_chosen(admin) -> None:
    """An unassigned application is ordinary under a shared queue."""
    assert can_approve(admin, _application(trainer=None)).allowed is True


def test_admin_cannot_approve_their_own_application(admin) -> None:
    """The admin override does not extend to deciding your own case."""
    own = SignupRequest.objects.create(
        email=admin.email, full_name=admin.full_name, user=admin, selected_trainer=None
    )
    assert can_approve(admin, own).allowed is False


def test_self_approval_message_names_the_real_reason(admin) -> None:
    """The self-approval rule must be reached before the general admin one.

    Both deny. If the admin check ran first, an admin who somehow reached the
    queue for their own application would be told "only admins may decide",
    which is both unhelpful and hides the rule that actually stopped them.
    """
    own = SignupRequest.objects.create(
        email=admin.email, full_name=admin.full_name, user=admin, selected_trainer=None
    )
    assert "own application" in can_approve(admin, own).reason


def test_none_cannot_approve(admin) -> None:
    assert can_approve(None, _application(trainer=None)).allowed is False


# ---------------------------------------------------------------------------
# Queue visibility
# ---------------------------------------------------------------------------


def test_only_approved_admins_see_the_queue(
    trainer, admin, approved_trainee, trainee
) -> None:
    """The queue is admin-only (D41). A trainer sees nothing, however senior."""
    assert can_view_queue(admin) is True
    assert can_view_queue(trainer) is False, "trainers are not approvers any more"
    assert can_view_queue(approved_trainee) is False, "trainees are not approvers"
    assert can_view_queue(trainee) is False, "pending users are inert"
    assert can_view_queue(None) is False


def test_admin_queue_sees_everything(admin, trainer, other_trainer) -> None:
    """One shared queue: every application, whoever it once named."""
    mine = _application(trainer=trainer)
    theirs = _application(trainer=other_trainer)
    unassigned = _application(trainer=None)

    visible = visible_requests(admin)

    assert {mine, theirs, unassigned} <= set(visible)


def test_queue_does_not_leak_to_a_non_approver(admin, trainer) -> None:
    """A trainer asking for the queue gets an empty set, not someone else's rows."""
    assert visible_requests(trainer) == []
    assert visible_requests(make_user("lurker@example.test", role=ROLE_TRAINER)) == []
    assert visible_requests(None) == []


# ---------------------------------------------------------------------------
# Cross-check: policies and the service must agree
# ---------------------------------------------------------------------------


def test_policy_allows_exactly_what_the_service_allows(admin, trainer) -> None:
    """`approval.py` delegates to `can_approve`; assert it still does.

    If someone re-inlines the check into the service, this pair of expectations
    is the thing that should notice.
    """
    from apps.accounts.service.approval import DecisionError, decide

    # approval requires a linked account, so this case needs a real applicant
    applicant = make_user("linked.applicant@example.test", role=ROLE_TRAINEE, approved=False)
    req = _application(applicant=applicant, trainer=trainer)

    # the policy said no, so the service must refuse and leave the row untouched
    assert can_approve(trainer, req).allowed is False
    with pytest.raises(DecisionError):
        decide(approver=trainer, request_pk=req.pk, approve=True)
    assert SignupRequest.objects.get(pk=req.pk).status == "pending"

    # the policy said yes, so the service must proceed
    assert can_approve(admin, req).allowed is True
    decide(approver=admin, request_pk=req.pk, approve=True)
    assert SignupRequest.objects.get(pk=req.pk).status == "approved"

    # and an admin can still handle a request that has no trainer at all
    unassigned_applicant = make_user(
        "unassigned.applicant@example.test", role=ROLE_TRAINEE, approved=False
    )
    unassigned = _application(applicant=unassigned_applicant, trainer=None)
    assert can_approve(admin, unassigned).allowed is True
    decide(approver=admin, request_pk=unassigned.pk, approve=False)
    assert SignupRequest.objects.get(pk=unassigned.pk).status == "rejected"


def test_approving_an_unlinked_application_is_refused(admin) -> None:
    """An application with no account behind it cannot be approved."""
    from apps.accounts.service.approval import DecisionError, decide

    orphan = _application(trainer=None)
    assert can_approve(admin, orphan).allowed is True  # policy allows it...

    with pytest.raises(DecisionError, match="not linked to an account"):
        decide(approver=admin, request_pk=orphan.pk, approve=True)  # ...service refuses

    assert SignupRequest.objects.get(pk=orphan.pk).status == "pending"


def test_approving_grants_the_requested_role_and_nothing_else(admin) -> None:
    """The regression, asserted at the service boundary.

    Approval used to grant ROLE_TRAINER unconditionally, which turned any
    trainee into an approver. Now it grants exactly the role the application
    asked for -- and a pending applicant has no role at all, so the *grant* is
    the only thing that adds one (D49).
    """
    from apps.accounts.service.approval import decide
    from apps.accounts.service.roles import current_roles

    applicant = make_user("will.be.promoted@example.test", role=None, approved=False)
    req = SignupRequest.objects.create(
        email=applicant.email,
        full_name=applicant.full_name,
        user=applicant,
        requested_role=RequestedRole.TRAINER,
    )
    assert current_roles(applicant) == [], "pending applicants hold no role"

    decide(approver=admin, request_pk=req.pk, approve=True)

    applicant.refresh_from_db()
    assert applicant.approval_status == "approved"
    assert sorted(applicant.role_assignments.values_list("role__slug", flat=True)) == [
        ROLE_TRAINER
    ]
    # The trainer role it asked for does not make it an approver. Under D41 that
    # authority belongs to admins alone, which is the whole point of the fix.
    assert can_approve(applicant, _application(trainer=applicant)).allowed is False


# ---------------------------------------------------------------------------
# Superusers
#
# `manage.py createsuperuser` grants is_superuser/is_staff and NO RoleAssignment.
# Every gate in this module is role-based, so the operator who can edit users in
# /admin/ was refused the approval queue outright. These pin the override -- and
# pin the limits of it, which are the part that matters.
# ---------------------------------------------------------------------------


@pytest.fixture
def superuser(db) -> User:
    """Exactly what createsuperuser produces: no application roles at all."""
    return make_user("root@example.test", is_superuser=True, is_staff=True)


def test_superuser_has_no_application_roles(superuser: User) -> None:
    """The premise. If this stops holding, the override is untested, not needed."""
    assert superuser.has_role(ROLE_TRAINER) is False
    assert superuser.has_role(ROLE_ADMIN) is False


def test_superuser_can_view_the_queue(superuser: User) -> None:
    assert is_approved(superuser) is True
    assert can_view_queue(superuser) is True


def test_superuser_sees_every_application_not_just_their_own(superuser: User, trainer: User) -> None:
    """A trainer sees only their own applicants; a superuser sees the lot."""
    mine = _application(trainer=trainer)

    listed = visible_requests(superuser)

    assert mine in listed, "the application was filtered out of the superuser's view"
    # Unfiltered, unlike a trainer's queue.
    assert len(listed) == SignupRequest.objects.count()


def test_superuser_can_decide_an_application_assigned_to_a_trainer(superuser: User, trainer: User) -> None:
    """Even one another trainer owns -- the backlog-clearing case."""
    req = _application(trainer=trainer)
    decision = can_approve(superuser, req)
    assert decision.allowed is True, decision.reason


def test_superuser_cannot_decide_their_own_application(superuser: User) -> None:
    """Separation of duties outranks the superuser override.

    Without the `trainer` role there is no self-application to exploit, so this
    is not currently reachable -- which is exactly why it is pinned. If a
    superuser is ever also given the trainer role, this must still refuse.
    """
    superuser.role_assignments.create(role=Role.objects.get(slug=ROLE_TRAINER))
    req = _application(applicant=superuser, trainer=superuser)

    decision = can_approve(superuser, req)
    assert decision.allowed is False
    assert "own application" in decision.reason


def test_inactive_superuser_is_refused(superuser: User) -> None:
    """is_superuser must not outlive deactivation."""
    superuser.is_active = False
    superuser.save(update_fields=["is_active"])

    assert is_superuser(superuser) is False
    assert can_view_queue(superuser) is False


def test_superuser_override_does_not_grant_trainer_facilities(superuser: User, trainer: User) -> None:
    """A superuser is an operator, not an approved LMS user.

    `is_approved` is the gate for trainee-facing capabilities such as
    registering on behalf of others, and the override is deliberately kept out
    of it.
    """
    unapproved = make_user("root2@example.test", approved=False, is_superuser=True)

    # Two separate things, and both have to hold. `is_superuser` is False here
    # because an unapproved account is inactive (D45), which is the same reason a
    # half-created operator confers nothing -- so this is not evidence about the
    # override at all. The override itself is the `superuser` fixture, who is
    # approved and active and therefore does pass.
    assert is_superuser(unapproved) is False
    assert is_superuser(superuser) is True
    assert is_approved(unapproved) is False
    assert can_register(unapproved).allowed is False

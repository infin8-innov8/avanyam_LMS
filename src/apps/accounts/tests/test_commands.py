"""`manage.py createadmin` -- the only path that mints an admin.

The command is not reachable from the web, so its guards are only exercised if
something calls it. That makes these tests the *entire* evidence for three
properties that would otherwise be documentation:

* an existing staff account authorises creating another admin
* `--bootstrap` works only while no staff account exists
* a refusal exits non-zero

That last one is the reason this file exists. The command's `_authorised()`
returned `False` and `handle` then returned, which is exit code 0 -- a deploy
script or a runbook line would record "admin created" for a run that created
nothing, and the failure would surface much later as someone unable to open the
approval queue.

The suite runs against a long-lived development database, so "no staff account
exists" cannot be arranged by deleting rows. `_authorised` is therefore tested
against an explicit queryset patch rather than by arranging global state.
"""

from __future__ import annotations

from unittest import mock

import pytest
from django.core.management import CommandError, call_command

from apps.accounts.domain.enums import ApprovalStatus, AuthSource
from apps.accounts.models import ROLE_ADMIN, ROLE_TRAINER, User

from .conftest import GOOD_PASSWORD, make_user, roles_of

pytestmark = pytest.mark.django_db


def call_createadmin(*, cleans_up: bool = True, **kwargs):
    """Invoke the command with the password prompts stubbed out.

    `getpass` is patched rather than fed because it reads from /dev/tty and a
    non-interactive run would hang. Everything else runs for real -- including
    `create_user` -- because the account the command leaves behind is most of what
    these tests need to assert, and mocking it out means asserting that a mock was
    called.

    Rows created here are removed afterwards. The suite runs against a long-lived
    development database, so a test that left a real admin behind would change the
    authorisation state of every test after it.
    """
    emails = [
        value
        for key, value in kwargs.items()
        if key in {"email"} and isinstance(value, str)
    ]
    with mock.patch(
        "apps.accounts.management.commands.createadmin.getpass.getpass",
        return_value=kwargs.pop("password", GOOD_PASSWORD),
    ):
        try:
            call_command("createadmin", **kwargs)
        finally:
            if cleans_up:
                User.objects.filter(email__in=emails).delete()


# ---------------------------------------------------------------------------
# The bootstrap guard
# ---------------------------------------------------------------------------


def test_bootstrap_is_refused_once_a_staff_account_exists(admin) -> None:
    """The load-bearing refusal.

    An earlier version short-circuited on `bootstrap or staff_exists`, so the flag
    was honoured unconditionally -- the exact inverse of what the docstring
    promised. On a deployment bootstrapped months earlier, a `--bootstrap` left in
    a runbook kept minting admins with no authorisation check at all, which is
    the one thing this command exists to prevent.
    """
    with pytest.raises(CommandError, match="no existing staff account authorises"):
        call_createadmin(
            bootstrap=True,
            email="smuggled@example.test",
            full_name="Smuggled",
        )


def test_refusing_to_bootstrap_creates_no_account(admin) -> None:
    """A refusal must not be a partial success."""
    with pytest.raises(CommandError):
        call_createadmin(
            bootstrap=True, email="half.made@example.test", full_name="Half Made"
        )

    assert not User.objects.filter(email="half.made@example.test").exists()


def test_bootstrap_succeeds_on_a_deployment_with_no_staff() -> None:
    """The legitimate use, and the reason the flag exists at all.

    The existence check is patched to empty rather than deleting real staff rows,
    so this does not disturb the shared development database. The account itself is
    created for real, because what it proves -- approved, active, holding the admin
    role -- cannot be asserted against a mock.
    """
    with mock.patch(
        "apps.accounts.management.commands.createadmin.User.objects.filter"
    ) as qs:
        qs.return_value.exists.return_value = False
        try:
            call_createadmin(
                cleans_up=False,
                bootstrap=True,
                email="first.admin@example.test",
                full_name="First Admin",
            )

            created = User.objects.get(email="first.admin@example.test")
            assert created.approval_status == ApprovalStatus.APPROVED
            assert created.is_active is True
            assert created.auth_source == AuthSource.LOCAL
            assert roles_of(created) == [ROLE_ADMIN]
        finally:
            User.objects.filter(email="first.admin@example.test").delete()


def test_no_bootstrap_and_no_staff_is_refused() -> None:
    """The default path cannot invent its own authority."""
    with (
        mock.patch(
            "apps.accounts.management.commands.createadmin.User.objects.filter"
        ) as qs,
        pytest.raises(CommandError, match="no existing staff account authorises"),
    ):
        qs.return_value.exists.return_value = False
        call_command(
            "createadmin", email="unauthorised@example.test", full_name="Unauthorised"
        )


def test_an_existing_staff_account_authorises_a_second_admin(admin) -> None:
    """The normal case: a second admin does not need the bootstrap flag."""
    call_createadmin(cleans_up=False, email="second.admin@example.test", full_name="Second Admin")

    try:
        assert User.objects.filter(email="second.admin@example.test").exists()
    finally:
        User.objects.filter(email="second.admin@example.test").delete()


# ---------------------------------------------------------------------------
# Refusals must be visible to the caller
# ---------------------------------------------------------------------------


def test_a_refusal_exits_non_zero(admin, capsys) -> None:
    """Exit code, not output, is what a script checks.

    `handle` used to `return` on refusal, which Django reports as success. The
    message was written to stderr and read by nobody.
    """
    with pytest.raises(CommandError):
        call_createadmin(
            bootstrap=True, email="quietly.refused@example.test", full_name="Quiet"
        )

    assert "bootstrap" in capsys.readouterr().err.lower()


# ---------------------------------------------------------------------------
# The account that is actually created
# ---------------------------------------------------------------------------


def test_the_created_admin_is_approved_and_active(admin) -> None:
    """Approved outright, because an admin gated on admin approval cannot bootstrap.

    Not a convenience: the first admin has no approver, so a pending admin is an
    account nobody can unlock.
    """
    created = User.objects.create_user(
        email="real.admin@example.test",
        password=GOOD_PASSWORD,
        full_name="Real Admin",
        auth_source=AuthSource.LOCAL,
        approval_status=ApprovalStatus.APPROVED,
    )

    assert created.approval_status == ApprovalStatus.APPROVED
    assert created.is_active is True


def test_the_created_admin_holds_the_admin_role(admin) -> None:
    created = make_user("role.admin@example.test", role=ROLE_ADMIN)
    assert roles_of(created) == [ROLE_ADMIN]


def test_an_admin_is_not_automatically_django_staff(admin) -> None:
    """`is_staff` gates /admin/, which is a different surface.

    An LMS admin manages approvals and roles. Granting Django admin-site access
    as a side effect would hand every approver the ability to edit users directly
    and bypass the approval queue entirely.
    """
    created = make_user("site.admin@example.test", role=ROLE_ADMIN)
    assert created.is_staff is False


def test_django_staff_without_the_lms_role_is_not_an_approver(admin) -> None:
    """The two roles are genuinely independent, in both directions."""
    staff_only = make_user("staff.only@example.test", is_staff=True)

    assert roles_of(staff_only) == []
    from apps.accounts.policies import can_view_queue

    assert can_view_queue(staff_only) is False


def test_a_lms_admin_without_staff_can_still_authorise(admin) -> None:
    """`createadmin` gates on `is_staff`, but LMS authority does not.

    Worth pinning because it looks like an inconsistency: an LMS admin who is
    not staff cannot authorise a new admin through this command, yet can approve
    accounts and manage roles through the web. The asymmetry is deliberate --
    this command is an operator action, not an in-app one.
    """
    lms_admin = make_user("lms.only@example.test", role=ROLE_ADMIN)

    assert lms_admin.is_staff is False
    from apps.accounts.policies import can_view_queue

    assert can_view_queue(lms_admin) is True


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_a_missing_email_is_refused(admin) -> None:
    with pytest.raises(CommandError, match="valid email"):
        call_createadmin(email="not-an-email", full_name="No Email")


def test_a_missing_full_name_is_refused(admin) -> None:
    with pytest.raises(CommandError, match="full name"):
        call_createadmin(email="nameless@example.test", full_name="  ")


def test_an_existing_address_is_refused_rather_than_overwritten(admin) -> None:
    """Re-running with a taken address must not mutate the existing account."""
    make_user("already.here@example.test", role=ROLE_TRAINER)

    with pytest.raises(CommandError, match="already exists"):
        call_command(
            "createadmin",
            email="already.here@example.test",
            full_name="Impostor",
        )

    existing = User.objects.get(email="already.here@example.test")
    assert existing.full_name != "Impostor"
    assert roles_of(existing) == [ROLE_TRAINER], "the existing role was disturbed"


def test_a_mismatched_password_confirmation_is_refused(admin) -> None:
    with (
        mock.patch(
            "apps.accounts.management.commands.createadmin.getpass.getpass",
            side_effect=["Corr3ct-Horse-Battery-9", "Something-Else-Entirely-1"],
        ),
        pytest.raises(CommandError, match="did not match"),
    ):
        call_command("createadmin", email="mismatch@example.test", full_name="Mismatch")


def test_an_empty_password_is_refused(admin) -> None:
    with (
        mock.patch(
            "apps.accounts.management.commands.createadmin.getpass.getpass",
            return_value="",
        ),
        pytest.raises(CommandError, match="password is required"),
    ):
        call_command("createadmin", email="nopass@example.test", full_name="No Pass")
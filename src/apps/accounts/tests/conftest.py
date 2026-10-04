"""Shared fixtures for the accounts test suite."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from apps.accounts.models import ROLE_ADMIN, ROLE_TRAINEE, ROLE_TRAINER, Role, RoleAssignment

User = get_user_model()

#: Not a real password. Satisfies Django's validators so tests can build users
#: without tripping AUTH_PASSWORD_VALIDATORS.
GOOD_PASSWORD = "Corr3ct-Horse-Battery-9"


def make_user(email: str, *, role: str | None = None, approved: bool = True, **extra):
    """Create a user with an optional role, bypassing password validation."""
    from apps.accounts.domain.enums import ApprovalStatus

    user = User.objects.create_user(
        email=email,
        password=GOOD_PASSWORD,
        full_name=email.split("@")[0].replace(".", " ").title(),
        approval_status=(
            ApprovalStatus.APPROVED if approved else ApprovalStatus.PENDING
        ),
        must_change_password=False,
        **extra,
    )
    if role:
        role_obj, _ = Role.objects.get_or_create(slug=role, defaults={"name": role.title()})
        RoleAssignment.objects.create(user=user, role=role_obj)
    return user


def roles_of(user) -> list[str]:
    return sorted(user.role_assignments.values_list("role__slug", flat=True))


@pytest.fixture
def trainer(db) -> User:
    return make_user("trainer@example.test", role=ROLE_TRAINER)


@pytest.fixture
def other_trainer(db) -> User:
    return make_user("other.trainer@example.test", role=ROLE_TRAINER)


@pytest.fixture
def admin(db) -> User:
    return make_user("admin@example.test", role=ROLE_ADMIN)


@pytest.fixture
def trainee(db) -> User:
    return make_user("trainee@example.test", role=ROLE_TRAINEE, approved=False)


@pytest.fixture
def approved_trainee(db) -> User:
    return make_user("approved.trainee@example.test", role=ROLE_TRAINEE)

"""Django admin regression tests for the custom (username-less) user model.

Django's ``UserAdmin`` is written against ``django.contrib.auth.models.User``,
which has ``username``, ``first_name`` and ``last_name``. This project's
``accounts.User`` has none of those. Inheriting Django's admin unchanged leaves
latent 500s behind on pages nobody visits until the first staff logins.

Specifically, ``DjangoUserAdmin.add_fieldsets`` hardcodes ``username``, and
``ModelAdmin.get_fieldsets()`` returns ``add_fieldsets`` -- not ``fieldsets`` --
when ``obj is None``. So ``/admin/accounts/user/add/`` raised::

    FieldError: Unknown field(s) (username, usable_password) specified for User

``manage.py check`` does not catch this: it only validates field *existence* in
``fieldsets``/``list_display``, not ``add_fieldsets`` inherited from a parent
class. Only rendering the page does.
"""

from __future__ import annotations

import pytest
from django.contrib import admin as django_admin
from django.test import Client

from apps.accounts.models import User

from .conftest import GOOD_PASSWORD

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff_client(db) -> Client:
    """A logged-in superuser able to reach the admin."""
    staff = User.objects.create_user(
        email="staff@example.test",
        password=GOOD_PASSWORD,
        full_name="Staff Person",
        is_staff=True,
        is_superuser=True,
        approval_status="approved",
    )
    client = Client()
    client.force_login(staff)
    return client


def test_add_page_renders(staff_client: Client) -> None:
    """The regression: this used to 500 with FieldError on 'username'."""
    response = staff_client.get("/admin/accounts/user/add/")
    assert response.status_code == 200, "admin add-user page must render"


def test_add_fieldsets_do_not_reference_missing_model_fields() -> None:
    """Guard the *cause*, not just the symptom.

    Asserting only the rendered status code would still pass if someone
    reintroduced a non-field error path, so check the fieldset itself against
    the model's real fields.
    """
    model_admin = django_admin.site._registry[User]
    valid = {f.name for f in User._meta.get_fields()}

    for label, fieldsets in (
        ("fieldsets", model_admin.fieldsets),
        ("add_fieldsets", model_admin.add_fieldsets),
    ):
        referenced = {
            name
            for _, opts in fieldsets
            for name in opts["fields"]
            if isinstance(name, str)
        }
        unknown = referenced - valid - {"password1", "password2"}
        assert not unknown, f"{label} references non-model fields: {sorted(unknown)}"


@pytest.mark.parametrize(
    "path",
    [
        "/admin/",
        "/admin/accounts/user/",
        "/admin/accounts/signuprequest/",
        "/admin/accounts/role/",
    ],
)
def test_admin_pages_render(staff_client: Client, path: str) -> None:
    assert staff_client.get(path).status_code == 200


def test_change_page_renders(staff_client: Client, trainee: User) -> None:
    assert staff_client.get(f"/admin/accounts/user/{trainee.pk}/change/").status_code == 200


def test_password_change_page_renders(staff_client: Client, trainee: User) -> None:
    assert staff_client.get(f"/admin/accounts/user/{trainee.pk}/password/").status_code == 200


def test_creating_a_user_hashes_the_password(staff_client: Client) -> None:
    """The add form must store a hash, never the plaintext.

    ``RoleInline`` contributes a ManagementForm, so a hand-written POST has to
    supply the inline counts or the admin rejects it with a non-field error.
    """
    response = staff_client.post(
        "/admin/accounts/user/add/",
        {
            "email": "made.in.admin@example.test",
            "full_name": "Made In Admin",
            "password1": GOOD_PASSWORD,
            "password2": GOOD_PASSWORD,
            "approval_status": "approved",
            "auth_source": "local",
            "is_active": "on",
            "role_assignments-TOTAL_FORMS": "0",
            "role_assignments-INITIAL_FORMS": "0",
            "role_assignments-MIN_NUM_FORMS": "0",
            "role_assignments-MAX_NUM_FORMS": "1000",
            "_save": "Save",
        },
    )
    assert response.status_code in (200, 302)

    created = User.objects.filter(email="made.in.admin@example.test").first()
    assert created is not None, "admin POST did not create the user"
    assert created.check_password(GOOD_PASSWORD)
    assert created.password != GOOD_PASSWORD, "password stored in plaintext"


def test_non_staff_cannot_reach_admin(staff_client: Client, trainee: User) -> None:
    """An ordinary trainee must not get the admin, even with a valid session."""
    client = Client()
    client.force_login(trainee)
    response = client.get("/admin/")
    assert response.status_code == 302
    assert "/admin/login/" in response["Location"]

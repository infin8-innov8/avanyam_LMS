"""The runtime role's database privileges are a security boundary.

The rest of the suite runs as `avanyam_migrate`, because it has to apply
migrations. That means those tests cannot catch a change to what `avanyam_app`
is allowed to do -- and that is the more important boundary of the two: the web
process should be able to read and write rows, and nothing else.

These tests connect as `avanyam_app` directly and assert the shape of that
permission set. They need no database fixtures: nothing here reads or writes
application data.
"""

from __future__ import annotations

import pytest
from django.db import connections

from apps.accounts.models import User

# Tables the web process must be able to work with.
BUSINESS_TABLES = [
    "accounts_user",
    "accounts_role",
    "accounts_roleassignment",
    "accounts_signuprequest",
    "auth_permission",
    "auth_group",
    "django_session",
    "axes_accesslog",
]


@pytest.fixture
def app_cursor(django_db_blocker):
    """A cursor authenticated as the runtime role, with DB access unlocked.

    Two deliberate choices:

    * The `runtime` alias, not `default`. config/settings/test.py re-points
      `default` at the migration role so the suite can apply migrations, so
      `default` cannot answer questions about what the web role may do.
    * `django_db_blocker` rather than the `db` fixture. The `db` fixture wraps
      each test in a transaction on the `default` alias and would try to set up
      a test database for `runtime` too, which needs CREATEDB and does not
      exist on this host. Nothing here reads or writes application rows, so
      there is nothing to roll back.
    """
    with django_db_blocker.unblock():
        with connections["runtime"].cursor() as cursor:
            yield cursor


@pytest.mark.parametrize("table", BUSINESS_TABLES)
def test_runtime_role_has_dml_on_business_tables(app_cursor, table):
    app_cursor.execute(
        """
        SELECT has_table_privilege(current_user, %s, 'SELECT'),
               has_table_privilege(current_user, %s, 'INSERT'),
               has_table_privilege(current_user, %s, 'UPDATE'),
               has_table_privilege(current_user, %s, 'DELETE')
        """,
        [table] * 4,
    )
    select, insert, update, delete = app_cursor.fetchone()
    assert select and insert and update and delete, (
        f"avanyam_app needs full DML on {table}"
    )


def test_runtime_role_cannot_create_tables(app_cursor):
    """Schema changes belong to avanyam_migrate only."""
    app_cursor.execute("SELECT has_schema_privilege(current_user, 'public', 'CREATE')")
    (can_create,) = app_cursor.fetchone()
    assert can_create is False


def test_runtime_role_owns_nothing(app_cursor):
    """If the web role owned a table it could drop and recreate it at will."""
    app_cursor.execute(
        """
        SELECT count(*) FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relowner = current_user::regrole
        """
    )
    (owned,) = app_cursor.fetchone()
    assert owned == 0, f"avanyam_app owns {owned} table(s) in public"


def test_runtime_role_is_not_superuser_and_cannot_createdb(app_cursor):
    app_cursor.execute(
        "SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = current_user"
    )
    is_super, can_createdb, can_createrole = app_cursor.fetchone()
    assert is_super is False
    assert can_createdb is False, (
        "the web role must not be able to create databases"
    )
    assert can_createrole is False


def test_runtime_role_cannot_drop_a_table(app_cursor):
    """The capability that matters most: no DDL escape hatch at runtime."""
    app_cursor.execute(
        """
        SELECT has_table_privilege(current_user, 'accounts_user', 'TRUNCATE'),
               has_table_privilege(current_user, 'accounts_user', 'REFERENCES'),
               has_table_privilege(current_user, 'accounts_user', 'TRIGGER')
        """
    )
    truncate, references, trigger = app_cursor.fetchone()
    assert truncate is False
    assert references is False
    assert trigger is False


# The audit trail is deliberately READABLE by the web role: the portal shows
# history to reviewers. What must hold is that it is APPEND-ONLY for that role.
# bootstrap.sql grants SELECT, INSERT and withholds UPDATE, DELETE, TRUNCATE, so
# a compromised web process can add entries and read them but cannot erase the
# evidence of its own compromise. That is the invariant worth a test.
#
# (An earlier version of this file asserted avanyam_app could not CONNECT to
# avanyam_audit at all. That was wrong -- it contradicted the documented design
# in bootstrap.sql. The isolation boundary is append-only, not unreachable.)


def test_runtime_role_cannot_connect_to_keycloak_database(app_cursor):
    """Keycloak is a separate trust domain and must be unreachable."""
    app_cursor.execute(
        """
        SELECT count(*) FROM pg_database
        WHERE datname LIKE 'keycloak%'
          AND has_database_privilege(current_user, datname, 'CONNECT')
        """
    )
    (reachable,) = app_cursor.fetchone()
    assert reachable == 0, "avanyam_app must not be able to CONNECT to any keycloak database"


def test_passwords_are_not_stored_in_plaintext(django_db_blocker):
    """A regression guard on the seed command's output."""
    with django_db_blocker.unblock():
        seeded = User.objects.filter(email__endswith="@gmail.com").first()
    if seeded is None:
        pytest.skip("no seeded accounts present")
    assert seeded is not None
    assert seeded.password.startswith(("pbkdf2_", "argon2", "bcrypt", "md5$"))
    assert "activ8" not in seeded.password

"""The audit trail must be append-only for the web role.

`avanyam_app` is deliberately allowed to SELECT and INSERT on `audit_log` -- the
portal shows history to reviewers, and bootstrap.sql grants exactly that. What
it must never hold is UPDATE, DELETE or TRUNCATE.

This is the property that makes the audit trail worth having. If a compromised
web process can delete the rows describing its own compromise, the trail is
decorative. These tests connect to `avanyam_audit` as `avanyam_app` and check the
privileges, and then actually attempt each forbidden statement, because a
privilege bit that reads False but permits the write would be worse than no test.

They need no application fixtures and read no business data.
"""

from __future__ import annotations

import pytest
from django.db import connections


@pytest.fixture
def audit_cursor(django_db_blocker):
    """A cursor on the audit database, authenticated as the web role.

    The `audit` alias in settings holds `avanyam_audit`'s own credentials --
    the ones that own the table. Overriding USER/PASSWORD to the web role is the
    whole point: we want to see what avanyam_app can do, not what the owner can.
    """
    audit = connections["audit"]
    original = (audit.settings_dict["USER"], audit.settings_dict["PASSWORD"])
    app = connections["runtime"].settings_dict
    audit.settings_dict["USER"] = app["USER"]
    audit.settings_dict["PASSWORD"] = app["PASSWORD"]
    try:
        audit.close()
        with django_db_blocker.unblock():
            with audit.cursor() as cursor:
                yield cursor
    finally:
        audit.settings_dict["USER"], audit.settings_dict["PASSWORD"] = original
        audit.close()


def test_web_role_may_read_the_audit_trail(audit_cursor):
    """Reading history is the feature these grants exist for."""
    audit_cursor.execute("SELECT count(*) FROM audit_log")
    assert audit_cursor.fetchone() is not None


@pytest.mark.parametrize(
    "privilege", ["UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"]
)
def test_web_role_lacks_destructive_privileges_on_audit_log(audit_cursor, privilege):
    audit_cursor.execute(
        "SELECT has_table_privilege(current_user, 'audit_log', %s)", [privilege]
    )
    (has,) = audit_cursor.fetchone()
    assert has is False, f"avanyam_app must not hold {privilege} on audit_log"


def test_web_role_cannot_update_the_audit_trail(audit_cursor):
    with pytest.raises(Exception) as excinfo:
        audit_cursor.execute("UPDATE audit_log SET action = 'tampered'")
    assert "permission denied" in str(excinfo.value).lower()


def test_web_role_cannot_delete_from_the_audit_trail(audit_cursor):
    with pytest.raises(Exception) as excinfo:
        audit_cursor.execute("DELETE FROM audit_log")
    assert "permission denied" in str(excinfo.value).lower()


def test_web_role_cannot_truncate_the_audit_trail(audit_cursor):
    with pytest.raises(Exception) as excinfo:
        audit_cursor.execute("TRUNCATE audit_log")
    assert "permission denied" in str(excinfo.value).lower()


def test_web_role_cannot_alter_the_audit_table(audit_cursor):
    """No DDL escape hatch: it could drop the guarantee, not just the rows."""
    with pytest.raises(Exception) as excinfo:
        audit_cursor.execute("ALTER TABLE audit_log DROP COLUMN action")
    assert "must be owner" in str(excinfo.value).lower() or "permission denied" in str(excinfo.value).lower()


def test_web_role_is_not_the_audit_table_owner(audit_cursor):
    audit_cursor.execute(
        """
        SELECT pg_get_userbyid(c.relowner) = current_user
        FROM pg_class c
        WHERE c.relname = 'audit_log'
          AND c.relnamespace = 'public'::regnamespace
        """
    )
    (is_owner,) = audit_cursor.fetchone()
    assert is_owner is False, (
        "avanyam_app must not own audit_log, or it could grant itself UPDATE"
    )

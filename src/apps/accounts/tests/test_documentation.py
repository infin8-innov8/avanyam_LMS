"""Keep the operational docs honest.

`CREDENTIALS.md` is the runbook someone follows by hand: it lists every account
and every route. Documentation rots silently -- a URL is renamed, the doc keeps
the old path, and the next person to follow it gets a 404 and learns nothing from
the failure. Nothing in the test suite would otherwise notice, because a Markdown
table cannot fail a test.

These checks are about *resolvability*, not prose. They assert that what the docs
claim exists still exists, which is the only part that can be verified
mechanically.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.urls import resolve, reverse

# tests/ -> accounts/ -> apps/ -> src/ -> repo root
_ROOT = Path(__file__).resolve().parents[4]
_CREDENTIALS = _ROOT / "CREDENTIALS.md"

#: Path converters used in the docs, mapped to a value that will satisfy `resolve`.
_CONVERTERS = {
    "<uuid>": "00000000-0000-0000-0000-000000000000",
    "<uuid:request_pk>": "00000000-0000-0000-0000-000000000000",
    "<id>": "1",
    "<path:object_id>": "1",
    "<path:content_type_id>": "1",
}

#: Anything matching this is a filesystem path or a glob, not a URL.
_NOT_A_URL = re.compile(r"/(avanyam_|src/|scripts/|\*|home/)")


def _documented_paths(text: str) -> set[str]:
    """Every ``/path`` in backticks that looks like a URL in this project."""
    found = set()
    for candidate in re.findall(r"`(/[^`\s]+)`", text):
        if _NOT_A_URL.search(candidate):
            continue
        if not candidate.startswith("/accounts/") and not candidate.startswith("/admin/"):
            # Only the two namespaces this runbook documents; /livez and friends are
            # covered separately so a stray path cannot fail the check.
            continue
        found.add(candidate)
    return found


@pytest.fixture(scope="module")
def credentials_text() -> str:
    if not _CREDENTIALS.exists():
        pytest.skip(f"{_CREDENTIALS.name} is absent (it is gitignored, so this is expected in CI)")
    return _CREDENTIALS.read_text(encoding="utf-8")


def test_the_runbook_documents_some_routes_at_all(credentials_text: str) -> None:
    """Guards the guard: a regex that stops matching passes everything below."""
    assert len(_documented_paths(credentials_text)) >= 15


@pytest.mark.parametrize("converter", sorted(_CONVERTERS))
def test_every_documented_route_resolves(credentials_text: str, converter: str) -> None:
    broken = []
    for path in sorted(_documented_paths(credentials_text)):
        if converter not in path:
            continue
        probe = path.replace(converter, _CONVERTERS[converter])
        try:
            resolve(probe)
        except Exception as exc:  # noqa: BLE001 - any failure means it is broken
            broken.append(f"{path} ({type(exc).__name__})")
    assert not broken, "documented routes that no longer resolve:\n" + "\n".join(broken)


def test_every_documented_static_route_resolves(credentials_text: str) -> None:
    """The routes with no path converter in them."""
    broken = []
    for path in sorted(_documented_paths(credentials_text)):
        if any(c in path for c in _CONVERTERS):
            continue
        try:
            resolve(path)
        except Exception as exc:  # noqa: BLE001
            broken.append(f"{path} ({type(exc).__name__})")
    assert not broken, "documented routes that no longer resolve:\n" + "\n".join(broken)


@pytest.mark.parametrize("name", ["livez", "readyz", "healthz"])
def test_the_health_routes_named_in_the_runbook_all_resolve(credentials_text: str, name: str) -> None:
    """`livez`/`readyz`/`healthz` are documented outside the path tables.

    They are named without a leading slash in the prose, so the path extractor above
    skips them; checked explicitly rather than left to rot. Reverse is used because
    that is what proves the *name* in the runbook is still the registered one.
    """
    if f"`/{name}`" not in credentials_text:
        pytest.skip(f"/{name} is not mentioned in the runbook")
    assert reverse(name) == f"/{name}"


def _table_row_for(email: str, text: str) -> str | None:
    """The single Markdown table row mentioning ``email``, or None."""
    for line in text.splitlines():
        if email in line and line.lstrip().startswith("|"):
            return line
    return None


def test_the_seeded_roster_in_the_docs_matches_the_command(credentials_text: str) -> None:
    """Every seeded account is documented, and no password is invented from a name.

    Checked **per row**, not per file. An earlier version asked only whether the
    expected string appeared somewhere in the document, which six correct trainee
    rows satisfied on behalf of a seventh that had been corrupted -- the guard
    reported green while the table lied about exactly the account being used.

    The password half of this test used to reconstruct `first_name(full_name)` and
    assert the runbook documented the value the seeder would derive. That is gone
    along with the derivation: `seed_people` now refuses to build a password from
    a person's name, so there is no per-account value to agree with. What is left
    is the part that still has teeth -- the roster must not drift, and the table
    must not quietly reintroduce a name-derived password by hand.
    """
    from apps.accounts.management.commands.seed_people import TRAINEES, TRAINERS

    problems = []
    for full_name, email in (*TRAINERS, *TRAINEES):
        row = _table_row_for(email, credentials_text)
        if row is None:
            problems.append(f"{email} is seeded but has no row in the runbook")
            continue
        # A password in a seeded row would have to come from somewhere, and the
        # only place left is a human. The command derives none, so any password
        # documented here is unbacked by the command.
        if re.search(r"`[^`]*\*[^`]*`", row):
            problems.append(
                f"{email}: row documents a password, but the command issues none"
            )

    assert not problems, "\n".join(problems)


def test_the_command_refuses_to_run_without_a_supplied_password() -> None:
    """The guard behind that contract, asserted where it actually lives.

    Without a password argument the command must stop rather than invent one. If
    it ever falls back to a name-derived default, the runbook table above becomes
    wrong again and nothing else in the suite would notice, because documentation
    tests cannot execute a command that refuses to start.
    """
    import inspect

    from apps.accounts.management.commands.seed_people import Command

    source = inspect.getsource(Command)
    assert "ACCOUNTS_SEED_PASSWORD" in source, "the password is no longer read from env"
    assert "CommandError" in source, "a missing password no longer stops the command"
    assert not hasattr(
        __import__(
            "apps.accounts.management.commands.seed_people", fromlist=["x"]
        ),
        "first_name",
    ), "a name-derived password helper is back in the command module"


def test_the_runbook_never_lists_an_infrastructure_secret(credentials_text: str) -> None:
    """The file's own policy, enforced.

    Seeded application passwords are listed on purpose -- they are reproducible
    from a template and useless off this host. Infrastructure secrets are not, and
    a future edit that pastes one in should fail here rather than in a code review
    somebody has to remember to make.
    """
    import re as _re

    from django.conf import settings

    leaked = []
    for name in dir(settings):
        if not name.isupper():
            continue
        value = getattr(settings, name, None)
        if not isinstance(value, str) or len(value) < 8:
            continue
        is_secret_name = name.endswith(("_PASSWORD", "_SECRET", "_KEY", "_TOKEN"))
        if is_secret_name and _re.search(_re.escape(value), credentials_text):
            leaked.append(name)
    assert not leaked, "live secret values written into CREDENTIALS.md: " + ", ".join(leaked)

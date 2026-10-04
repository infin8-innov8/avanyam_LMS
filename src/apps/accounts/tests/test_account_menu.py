"""The avatar and the account menu that hangs off it.

Replaces a "My account" link and a "Sign out" button that appeared, in those
words, in the masthead of every single page. The rules worth pinning:

  * `initials` is derived, never stored, and never returns something silly for a
    one-word name, a run of whitespace or a blank name -- the avatar is 34px
    wide and an empty circle tells nobody who they are,
  * the menu opens with JavaScript blocked, because its control is a checkbox,
  * signing out is still a POST carrying a CSRF token, and
  * the avatar carries a real accessible name, so it is not announced as "AR".
"""

from __future__ import annotations

from pathlib import Path

import pytest
from django.urls import reverse

from apps.accounts.tests.conftest import make_user

CSS = (
    Path(__file__).resolve().parents[3] / "frontend" / "static" / "css" / "avanyam.css"
).read_text()

#: Every page that renders the authenticated masthead: the URL, the status it
#: answers with, and the role it needs. The last entry is the 404 body a
#: non-approver gets from the admin queue -- still a full page with a masthead, and
#: it used to carry a second copy of the sign-out button.
AUTHENTICATED_PAGES = [
    ("accounts:home", 200, "trainee"),
    ("accounts:password-change", 200, "trainee"),
    ("accounts:admin-queue", 200, "admin"),
    ("accounts:admin-queue", 404, "trainee"),  # the same URL, seen without the role
]


def _user(email: str, full_name: str):
    """`make_user` derives `full_name` from the email and takes it positionally,
    so it cannot be overridden through **extra. Set it after: `initials` reads
    the attribute, not the database."""
    user = make_user(email)
    user.full_name = full_name
    return user


def _sign_in(client, user) -> None:
    client.force_login(user)


def _nav(client, url_name: str, status: int = 200) -> str:
    """Just the `<nav>` out of a rendered page, so these assertions cannot be
    satisfied by the same words appearing in the page body."""
    response = client.get(reverse(url_name))
    assert response.status_code == status
    html = response.content.decode()
    start = html.index("<nav")
    return html[start : html.index("</nav>", start)]


# ----------------------------------------------------------------- initials


@pytest.mark.parametrize(
    ("full_name", "expected"),
    [
        ("Aniket Rohokale", "AR"),
        ("Ada Lovelace", "AL"),
        # A middle name is not part of the initials; first and last are.
        ("Aniket Kumar Rohokale", "AR"),
        ("cher", "C"),
        ("Prince", "P"),
        ("  ada   lovelace  ", "AL"),
    ],
)
def test_initials_are_the_first_and_last_letter(db, full_name, expected) -> None:
    user = _user("someone@example.test", full_name)

    assert user.initials == expected


def test_initials_are_upper_case_whatever_the_name_was(db) -> None:
    assert _user("a@example.test", "aDA lOVELACE").initials == "AL"


@pytest.mark.parametrize("full_name", ["", "   ", "\t\n"])
def test_initials_fall_back_to_the_email_when_the_name_is_blank(db, full_name) -> None:
    """`full_name` is required, but both the signup form and the admin reach the
    column, and an empty circle is worse than a letter."""
    user = _user("zoe@example.test", full_name)

    assert user.initials == "Z"


def test_initials_never_leak_the_email(db) -> None:
    """The avatar is chrome on every page. Two letters, never the address."""
    user = _user("private.person@example.test", "Private Person")

    assert user.initials == "PP"
    assert "private.person" not in user.initials


# ------------------------------------------------------------- the chrome


@pytest.mark.parametrize(("url_name", "status", "role"), AUTHENTICATED_PAGES)
def test_the_avatar_appears_on_every_authenticated_page(
    client, approved_trainee, admin, url_name, status, role
) -> None:
    signed_in = admin if role == "admin" else approved_trainee
    _sign_in(client, signed_in)
    nav = _nav(client, url_name, status)

    assert 'class="usermenu"' in nav
    assert signed_in.initials in nav


def test_the_avatar_carries_the_name_not_just_the_letters(client, approved_trainee) -> None:
    """Two letters read as "AP" to a screen reader, which identifies nobody. The
    letters are hidden from it and the name is supplied instead."""
    _sign_in(client, approved_trainee)
    nav = _nav(client, "accounts:home")

    assert '<span class="avatar__letters" aria-hidden="true">' in nav
    assert (
        f'<span class="sr-only">Account menu for {approved_trainee.full_name}</span>' in nav
    )


def test_the_my_account_link_is_gone(client, approved_trainee) -> None:
    """It was the same two words in the masthead of every page, pointing at a
    page the wordmark already links to."""
    _sign_in(client, approved_trainee)

    assert "My account" not in _nav(client, "accounts:home")


def test_the_menu_signs_out_with_a_post_and_a_csrf_token(client, approved_trainee) -> None:
    """A GET link would let any page on the internet sign this person out with
    an `<img>` tag, and there would be no token to check."""
    _sign_in(client, approved_trainee)
    nav = _nav(client, "accounts:home")

    assert f'action="{reverse("accounts:logout")}"' in nav
    assert 'method="post"' in nav
    assert "csrfmiddlewaretoken" in nav
    assert "Sign out" in nav


def test_the_menu_opens_without_javascript(client, approved_trainee) -> None:
    """The control is a checkbox, so the browser does the toggling. Anything
    script-driven would leave the panel unreachable with portal.js blocked."""
    _sign_in(client, approved_trainee)
    nav = _nav(client, "accounts:home")

    assert (
        '<input class="usermenu__toggle" type="checkbox" id="usermenu-toggle">' in nav
    )
    assert 'for="usermenu-toggle"' in nav
    assert "<script" not in nav


def test_the_panel_is_still_a_sibling_of_the_control_that_shows_it() -> None:
    """Guards the pairing: a `for=` pointing nowhere, or a sibling selector that
    no longer matches, leaves a permanently visible panel over the page."""
    assert ".usermenu__toggle:checked ~ .usermenu__panel" in CSS
    # Hidden unconditionally, so a reduced-motion visitor still gets a closed menu.
    assert "visibility: hidden;" in CSS


def test_the_masthead_is_allowed_to_wrap() -> None:
    """It was `nowrap`. At 320px the wordmark plus the nav is 395px wide, so the
    avatar ended up at x=361-395, entirely off the right edge of the screen,
    taking the menu with it."""
    assert "flex-wrap: wrap;" in CSS


def test_the_panel_is_capped_so_a_long_address_cannot_stretch_it() -> None:
    """`inline-size: max-content` alone made the dropdown as wide as the longest
    email in the database -- 461px for a 62-character address."""
    assert "max-inline-size: min(260px, calc(100vw - 2 * var(--sp-5)));" in CSS


def test_a_signed_out_visitor_gets_the_public_links_and_no_avatar(client) -> None:
    nav = _nav(client, "accounts:login", 200)

    assert "usermenu" not in nav
    assert "Sign in" in nav
    assert "Register" in nav


def test_an_admin_keeps_the_approvals_link(client, admin) -> None:
    """Only the account link went. Approvals is the reason an admin signs in."""
    _sign_in(client, admin)

    assert "Approvals" in _nav(client, "accounts:admin-queue", 200)


def test_a_trainer_is_offered_no_approvals_link(client, trainer) -> None:
    """A trainer still gets the masthead, just without a queue they cannot open.

    The nav is not the enforcement -- the view is -- but a link that always 404s
    reads as a broken site, and it would be the only thing telling a trainer the
    permission had moved.
    """
    _sign_in(client, trainer)
    nav = _nav(client, "accounts:home", 200)

    assert "Approvals" not in nav
    assert reverse("accounts:admin-queue") not in nav


def test_the_no_queue_page_no_longer_repeats_the_sign_out_button(client, approved_trainee) -> None:
    """It had its own "Go to my account" and "Sign out" pair, which meant two
    sign-out controls on one screen and a second route to the account page.

    Now the 404 a trainee gets from the admin queue: still a full page, still
    carrying exactly one sign-out control."""
    _sign_in(client, approved_trainee)
    response = client.get(reverse("accounts:admin-queue"))

    assert response.status_code == 404
    body = response.content.decode()
    body = body[body.index("</nav>") :]
    assert "Sign out" not in body
    assert "Go to my account" not in body
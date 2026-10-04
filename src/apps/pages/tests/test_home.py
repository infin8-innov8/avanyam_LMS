"""The public front door: what it serves, and what it refuses to become.

Two kinds of assertion here. The first group is behavioural and fails loudly if
the page breaks. The second pins properties that regress silently -- the
reveal animation staying inside a reduced-motion guard, the role switcher
staying a CSS-only mechanism, and the rendered copy staying free of the dashes
that turn a page into a wall of typographic noise. Those are the failures that
still look fine in a screenshot.
"""

from __future__ import annotations

import re

import pytest
from django.conf import settings
from django.urls import reverse

from apps.accounts.domain.enums import ApprovalStatus
from apps.accounts.models import User

HOME = "/"
LOGIN = "/accounts/login/"
SIGNUP = "/accounts/signup/"

#: U+2014 and U+2013.
DASHES = ("—", "–")

ROLE_IDS = ("hp-role-trainee", "hp-role-trainer", "hp-role-admin", "hp-role-platform")


def _home_template() -> str:
    return (settings.BASE_DIR / "src/frontend/templates/pages/home.html").read_text()


def _home_css() -> str:
    return (settings.BASE_DIR / "src/frontend/static/css/home.css").read_text()


def _home_js() -> str:
    return (settings.BASE_DIR / "src/frontend/static/js/home.js").read_text()


@pytest.fixture
def home_html(client) -> str:
    return client.get(HOME).content.decode()


# ------------------------------------------------------------------ it serves


def test_the_root_serves_the_home_page(client) -> None:
    response = client.get(HOME)

    assert response.status_code == 200
    assert "pages/home.html" in [template.name for template in response.templates]


def test_the_root_is_no_longer_a_redirect_to_sign_in(client) -> None:
    """It used to be a RedirectView to the login page; this pins the change."""
    response = client.get(HOME, follow=False)

    assert not response.get("Location")


def test_the_home_page_offers_both_ways_in(home_html: str) -> None:
    assert f'href="{LOGIN}"' in home_html
    assert f'href="{SIGNUP}"' in home_html


def test_the_home_page_never_advertises_a_password_reset(home_html: str) -> None:
    """No such route exists (the gap documented in CREDENTIALS.md), so a link
    to one would be a dead end on the first page a visitor ever sees."""
    assert "/accounts/password" not in home_html
    assert "reset" not in home_html.lower()


def test_the_home_page_names_the_four_roles_from_the_brief(home_html: str) -> None:
    for role in ("Trainee", "Trainer", "Administrator", "IT and platform"):
        assert role in home_html


@pytest.mark.django_db
def test_an_authenticated_visitor_still_gets_the_page(client) -> None:
    user = User.objects.create_user(
        email="reader@example.com",
        password="A-test-only-password-9",
        full_name="Reader Example",
        approval_status=ApprovalStatus.APPROVED,
    )
    client.force_login(user)

    response = client.get(HOME)

    assert response.status_code == 200
    # The masthead's signed-in branch, checked through the avatar rather than the
    # account link that used to sit there.
    assert "usermenu" in response.content.decode()
    assert user.initials in response.content.decode()


def test_the_home_page_is_not_cacheable(client) -> None:
    """The masthead renders the signed-in nav, so a shared cache entry for `/`
    could serve one visitor the chrome belonging to another."""
    cache_control = client.get(HOME).headers.get("Cache-Control", "")

    assert "no-store" in cache_control
    assert "private" in cache_control


def test_the_home_page_reverse_name_is_stable() -> None:
    assert reverse("pages:home") == HOME


# --------------------------------------------------- progressive enhancement


def _enclosing_at_rules(css: str, needle: str) -> list[str]:
    """Every unclosed `{ ... }` block whose text contains `needle`.

    Brace-matched rather than a substring search, because the property that
    matters here is not "the media query appears first in the file" but "the
    rule is nested inside it". Matches selectors and declarations alike, so
    either can be handed in as the needle.
    """
    stack: list[str] = []
    enclosing: list[str] = []
    header = ""
    body = ""

    for char in css:
        if char == "{":
            header = body.strip()
            body = ""
            stack.append(header)
        elif char == "}":
            if needle in header + body:
                enclosing = list(stack)
            header = ""
            body = ""
            if stack:
                stack.pop()
        else:
            body += char

    return enclosing


def test_the_scroll_reveals_are_gated_behind_javascript_and_motion_preference() -> None:
    """The hidden starting state must not exist for a browser that cannot
    animate or cannot dismiss anything, or the page renders blank."""
    enclosing = _enclosing_at_rules(_home_css(), ".js .hp-reveal")

    assert any("prefers-reduced-motion: no-preference" in block for block in enclosing), (
        "the reveal gate is not inside a reduced-motion guard: "
        + " > ".join(enclosing)
    )


def test_the_scroll_reveals_use_an_observer_and_not_a_scroll_listener() -> None:
    js = _home_js()

    assert "IntersectionObserver" in js
    assert 'addEventListener("scroll"' not in js
    assert 'addEventListener("wheel"' not in js


def test_the_role_switcher_needs_no_javascript(home_html: str) -> None:
    """A radio group plus `:checked ~` rules in the stylesheet. If somebody
    reaches for a tab widget instead, this is where it gets caught."""
    radios = re.findall(r'<input class="hp-radio" type="radio" name="hp-role"', home_html)
    panels = re.findall(r'class="hp-panel hp-panel--([a-z]+)"', home_html)

    assert len(radios) == len(ROLE_IDS)
    assert sorted(panels) == ["admin", "platform", "trainee", "trainer"]


@pytest.mark.parametrize("role_id", ROLE_IDS)
def test_every_role_has_a_label_and_a_checked_rule(role_id: str) -> None:
    """Four id-linked pairs. A renamed id in the template with no matching rule
    in the stylesheet hides that panel for good."""
    assert f'for="{role_id}"' in _home_template()
    assert f"#{role_id}:checked" in _home_css()


def test_the_panels_are_stacked_so_switching_crossfades_instead_of_blinking() -> None:
    """`display` on a panel makes the swap blink and throws away the transition,
    because there is no box left to animate. They share one grid cell instead."""
    css = _home_css()

    assert "grid-area: 1 / 1;" in css
    assert ".hp-panel { display: none; }" not in css
    assert not re.search(r"\.hp-panel[^{]*\{[^}]*\bdisplay:\s*(none|block)\b", css)


def test_only_the_checked_panel_is_left_in_the_accessibility_tree() -> None:
    """Fading to zero opacity is not enough. `visibility: hidden` also keeps a
    panel out of the a11y tree and out of the tab order, and it must be
    unconditional so reduced-motion visitors still get exactly one panel."""
    css = _home_css()

    assert "visibility: hidden;" in css
    # The rule block that declares it counts as enclosing; what must not appear
    # is an at-rule, which would put the hiding behind a media query.
    assert not [
        block for block in _enclosing_at_rules(css, "visibility: hidden;") if block.startswith("@")
    ]
    assert "visibility: visible;" in css


def test_the_swap_motion_is_gated_behind_a_motion_preference() -> None:
    enclosing = _enclosing_at_rules(_home_css(), "transform: translateY(8px);")

    assert enclosing, "the swap offset is not declared at all"
    assert any("prefers-reduced-motion: no-preference" in block for block in enclosing)


def test_the_checked_panel_resets_the_swap_offset_and_the_visibility_delay() -> None:
    """Getting this wrong leaves the visible panel sitting 8px low, and makes the
    outgoing one vanish from under the finger instead of fading out."""
    css = _home_css()
    shown = re.search(
        r"#hp-role-trainee:checked\s+~\s+\.hp-panels \.hp-panel--trainee,[^}]*\}",
        css,
    )

    assert shown, "the checked panel rule is gone"
    assert "transform: none;" in shown.group(0)
    assert "--hp-vis-delay: 0s;" in shown.group(0)


def test_every_in_page_navigation_target_exists(home_html: str) -> None:
    targets = re.findall(r'<a href="#([a-z-]+)">', home_html)

    assert targets, "the in-page navigation rendered no links"
    for target in targets:
        assert f'id="{target}"' in home_html, f"#{target} is linked but not on the page"


def test_the_hero_image_is_sized_so_it_cannot_shift_the_layout(home_html: str) -> None:
    """No width and height means the fold moves when the JPEG lands, which is a
    layout shift on the one screen that has to feel instant."""
    assert 'width="1280"' in home_html
    assert 'height="763"' in home_html
    assert "alt=\"" in home_html


# ---------------------------------------------------------------- copy hygiene


def test_the_rendered_copy_contains_no_em_or_en_dashes(home_html: str) -> None:
    for dash in DASHES:
        assert dash not in home_html


def test_the_copy_source_contains_no_em_or_en_dashes() -> None:
    """Checked at the source too, so a failure names the file that caused it."""
    body = re.sub(r"\{%.*?%\}", "", _home_template(), flags=re.DOTALL)

    for dash in DASHES:
        assert dash not in body


def test_the_copy_makes_no_claim_the_brief_does_not_support(home_html: str) -> None:
    """prd.md §6 puts paid commerce, user-to-user messaging and native mobile
    apps out of scope for v1. A landing page is exactly where those creep in."""
    for claim in ("Buy", "Enroll now", "Download the app", "Message your trainer"):
        assert claim not in home_html
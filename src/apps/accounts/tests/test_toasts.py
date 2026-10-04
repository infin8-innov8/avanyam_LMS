"""Every screen reports failure the same way: a toast, server-rendered.

These are template-level assertions, so they are deliberately blunt -- markup
presence, role, and the wiring that keeps the visible countdown honest. They
exist because the behaviour spans three files that no single unit test would
otherwise tie together: base.html renders it, avanyam.css animates it against
`--toast-ms`, and portal.js reads that same value back to schedule removal.
"""

from __future__ import annotations

import re

import pytest
from django.conf import settings

from apps.accounts.models import SignupRequest

from .conftest import make_user

pytestmark = pytest.mark.django_db

LOGIN_URL = "/accounts/login/"
SIGNUP_URL = "/accounts/signup/"
QUEUE_URL = "/accounts/admin/queue/"

TIMER = 'class="toast__timer"'


def _static(*parts: str):
    """A file under src/frontend/static, located from settings rather than
    relative to this test, so the path does not depend on the test's depth."""
    return settings.BASE_DIR.joinpath("src", "frontend", "static", *parts)


def _css() -> str:
    return _static("css", "avanyam.css").read_text()


def _js() -> str:
    return _static("js", "portal.js").read_text()


def _toast_blocks(html: str) -> list[str]:
    return re.findall(r'<div class="toast .*?</div>', html, flags=re.DOTALL)


def _toast_count(html: str) -> int:
    return html.count('<div class="toast ')


# ---------------------------------------------------------------------------
# The countdown is visible, and it is the same number the timer uses
# ---------------------------------------------------------------------------


def _failed_login_html(client) -> str:
    """The response to a rejected login.

    PortalLoginView sets no session message on failure -- Django's form_invalid
    just re-renders the bound form -- so the toast has to be asserted on this
    response. A later GET would be a clean page and would prove nothing.
    """
    response = client.post(
        LOGIN_URL, {"username": "nobody@example.test", "password": "wrong"}
    )
    assert response.status_code == 200, "a rejected login should re-render the form"
    return response.content.decode()


def test_every_toast_carries_a_visible_countdown(client) -> None:
    """The remaining time should be legible, not merely implied."""
    blocks = _toast_blocks(_failed_login_html(client))

    assert blocks, "a failed login produced no toast"
    assert all(TIMER in block for block in blocks), "a toast is missing its timer track"


def test_the_duration_is_defined_once_in_css() -> None:
    """One source of truth.

    If the duration were also hardcoded in JS it would eventually disagree with
    the bar's animation, and the countdown would start lying about the time left.
    """
    assert "--toast-ms:" in _css(), "CSS no longer defines the toast duration"
    assert "--toast-ms" in _js(), "JS no longer reads the duration from CSS"

    toast_section = _js().split("4. toasts")[-1]
    assert not re.search(r"\b\d{4,}\s*[,)]", toast_section), (
        "portal.js hardcodes a millisecond duration instead of reading --toast-ms"
    )


def test_the_drain_is_animated_against_that_duration() -> None:
    """The bar and the timer have to be driven by the same property."""
    css = _css()

    assert "toast-drain var(--toast-ms)" in css, "the bar does not animate on --toast-ms"
    assert "@keyframes toast-drain" in css


# ---------------------------------------------------------------------------
# Errors are announced, not merely coloured
# ---------------------------------------------------------------------------


def test_a_login_error_actually_produces_a_toast(client) -> None:
    response = client.post(
        LOGIN_URL, {"username": "nobody@example.test", "password": "wrong"}, follow=True
    )

    assert response.status_code == 200
    assert _toast_count(response.content.decode()) >= 1


def test_an_error_toast_is_an_alert(client) -> None:
    assert 'role="alert"' in _failed_login_html(client), (
        "error toast is not announced assertively"
    )


def test_form_errors_produce_a_toast_and_are_still_inline(client) -> None:
    """The rule that shaped the design.

    A toast may be what *grabs attention*; it must never be the only copy of a
    message the user has to act on. So it is added to the inline error, never
    substituted for it.
    """
    html = client.post(SIGNUP_URL, {"email": "not-an-email"}, follow=True).content.decode()

    assert _toast_count(html) >= 1, "invalid signup produced no toast"
    assert "Please correct" in html, "no human-readable summary in the toast"
    assert 'class="errorlist"' in html, "the inline field error was dropped"


def test_a_persistent_error_toast_does_not_expire_on_its_own(client) -> None:
    """It must not scroll away from someone who was slow to look."""
    html = client.post(SIGNUP_URL, {"email": "not-an-email"}, follow=True).content.decode()

    bad = [block for block in _toast_blocks(html) if "--bad" in block]

    assert bad, "the error toast is missing"
    assert all("data-toast--persistent" in block for block in bad)


def test_a_clean_page_has_no_toast_region(client) -> None:
    assert 'id="toasts"' not in client.get(LOGIN_URL).content.decode(), (
        "an empty toast stack is being rendered"
    )


# ---------------------------------------------------------------------------
# Success messages get the same treatment, politely
# ---------------------------------------------------------------------------


def test_an_approval_outcome_toasts_to_the_admin(client, admin) -> None:
    # The request must be linked to a real account: decide() refuses to approve
    # an orphan ("not linked to an account"), which would turn this into a test
    # of the error path.
    applicant = make_user("toasted@example.test", approved=False)
    req = SignupRequest.objects.create(
        email=applicant.email,
        full_name="Toasted Applicant",
        user=applicant,
    )
    client.force_login(admin)

    html = client.post(
        f"{QUEUE_URL}{req.pk}/", {"decision": "approve"}, follow=True
    ).content.decode()

    assert "Toasted Applicant" in html, "the outcome message is missing"
    assert 'role="status"' in html, "a success toast should be polite, not assertive"
    assert not [b for b in _toast_blocks(html) if "data-toast--persistent" in b], (
        "a success message must be allowed to expire"
    )


def test_a_refused_decision_toasts_the_reason(client, admin) -> None:
    """The error path, end to end: the view's DecisionError becomes a toast."""
    orphan = SignupRequest.objects.create(
        email="orphan@example.test",
        full_name="Orphan Applicant",
    )
    client.force_login(admin)

    html = client.post(
        f"{QUEUE_URL}{orphan.pk}/", {"decision": "approve"}, follow=True
    ).content.decode()

    assert "not linked to an account" in html
    assert 'role="alert"' in html
    assert [b for b in _toast_blocks(html) if "data-toast--persistent" in b], (
        "an error the user must act on must not expire on its own"
    )


# ---------------------------------------------------------------------------
# Progressive enhancement: the message must survive with JS disabled
# ---------------------------------------------------------------------------


def test_the_toast_text_is_in_the_html_not_injected_by_script(client) -> None:
    assert '<p class="toast__text">' in _failed_login_html(client), (
        "toast text is not server-rendered"
    )


def test_the_html_carries_a_js_flag_so_the_countdown_can_be_gated(client) -> None:
    """Without it the bar would animate on a browser that cannot dismiss it."""
    html = client.get(LOGIN_URL).content.decode()

    assert 'class="no-js"' in html
    assert 'js");' in html, "the no-js -> js swap script is missing"


def test_the_dismiss_control_is_a_real_button_with_a_label(client) -> None:
    html = _failed_login_html(client)

    assert "data-toast-close" in html
    assert 'aria-label="Dismiss"' in html


def test_the_dismiss_button_is_reachable_by_keyboard(client) -> None:
    """It is a <button>, not a styled div, so it is in the tab order."""
    html = _failed_login_html(client)

    close_buttons = re.findall(r"<button[^>]*data-toast-close[^>]*>", html)

    assert close_buttons, "no dismiss control rendered"
    assert all('type="button"' in tag for tag in close_buttons), (
        "a dismiss control with type=button would submit the surrounding form"
    )


# ---------------------------------------------------------------------------
# The overlay must not eat the page
# ---------------------------------------------------------------------------


def test_the_stack_only_accepts_clicks_on_the_toasts_themselves() -> None:
    """A fixed overlay that swallows clicks is a bug, not a feature."""
    css = _css()

    stack = re.search(r"\.toasts \{.*?\}", css, flags=re.DOTALL)
    single = re.search(r"\.toast \{.*?\}", css, flags=re.DOTALL)

    assert stack and single, "toast rules are missing"
    assert "pointer-events: none" in stack.group(0), "the stack blocks the page"
    assert "pointer-events: auto" in single.group(0), "toasts are not clickable"


def test_toasts_are_positioned_above_the_sticky_header() -> None:
    """Otherwise the masthead swallows them."""
    css = _css()

    stack = re.search(r"\.toasts \{.*?\}", css, flags=re.DOTALL).group(0)
    header_z = int(re.search(r"z-index: (\d+);", css).group(1))

    toast_z = int(re.search(r"z-index: (\d+);", stack).group(1))
    assert toast_z > header_z, f"toasts at z-index {toast_z} sit under the header ({header_z})"


def test_reduced_motion_keeps_the_countdown_but_drops_the_slide() -> None:
    """The drain is information; the slide is decoration."""
    css = _css()
    toast_css = css[css.index("/* ------------------------------------------------------------------ toasts") :]
    reduced = re.search(
        r"@media \(prefers-reduced-motion: reduce\) \{(.*?)\n  \}", toast_css, flags=re.DOTALL
    )
    assert reduced, "no reduced-motion block for toasts"

    block = reduced.group(1)
    assert "toast-drain var(--toast-ms)" in block, "the countdown was disabled entirely"
    assert re.search(r"\.js \.toast, .*animation: none", block), (
        "arrival motion is not suppressed under reduced motion"
    )


# ---------------------------------------------------------------------------
# A template comment must never reach the browser
#
# This exists because it already happened. Django's single-brace comment {# #}
# is compiled from a pattern that is not DOTALL, so a newline inside it defeats
# the lexer and the entire block is emitted as literal visible text. The page
# still rendered, every assertion about toasts still passed, and each visitor
# read a paragraph of developer notes above the login form.
# ---------------------------------------------------------------------------


def test_the_page_never_leaks_a_template_comment(client) -> None:
    for url in (LOGIN_URL, SIGNUP_URL):
        html = client.get(url).content.decode()

        # A raw opener or closer in the *output* means the lexer gave up. These
        # two may legitimately appear inside a comment block's own text, so they
        # are checked outside of one rather than banned outright.
        body = re.sub(r"\{% comment %\}.*?\{% endcomment %\}", "", html, flags=re.DOTALL)
        assert "{#" not in body, f"{url} is emitting a raw template comment opener"
        assert "#}" not in body, f"{url} is emitting a raw template comment closer"
        assert "{% comment %}" not in html, f"{url} is emitting a comment block tag"


def test_the_specific_wording_of_the_old_leak_is_gone(client) -> None:
    """Named so the failure says what happened, not just that something did."""
    html = client.get(LOGIN_URL).content.decode()

    for phrase in ("browser that cannot dismiss", "Two sources feed it", "Toasts are"):
        assert phrase not in html, f"developer commentary is visible to users: {phrase!r}"


def test_django_really_does_reject_a_multiline_single_brace_comment() -> None:
    """Why the test above exists, asserted against Django itself.

    If a future Django release fixes the lexer, this fails and the guard above
    can be relaxed deliberately rather than by accident.
    """
    from django.template import Context, Template

    rendered = Template("{#\n  a comment\n  over two lines\n#}").render(Context({}))

    assert rendered.strip() != "", "Django now strips multi-line {# #}; the guard can go"


def test_the_multiline_comment_construct_is_actually_stripped(client) -> None:
    """The fix, asserted directly: {% comment %} handles newlines."""
    from django.template import Context, Template

    rendered = Template("{% comment %}\n  hidden\n{% endcomment %}ok").render(Context({}))

    assert rendered == "ok"


# ---------------------------------------------------------------------------
# The stack is centred, because the corner placement covered the nav
#
# Pinned in CSS terms rather than by geometry: a real layout assertion would need
# a browser, and the failure this guards against is a one-line CSS edit that
# quietly puts the stack back over the Sign out button.
# ---------------------------------------------------------------------------


def test_the_stack_is_centred_on_the_inline_axis() -> None:
    css = _css()
    block = css[css.index(".toasts {") : css.index(".toasts {") + 700]

    assert "inset-inline: 0;" in block, "the stack is not spanning and centring"
    assert "margin-inline: auto;" in block
    assert "inset-inline-end" not in block, (
        "anchoring one edge puts the stack back over the nav controls"
    )
    assert "transform" not in block, (
        "transform here would fight the toast entrance animation, which uses it"
    )


def test_the_stack_still_lets_clicks_through_to_the_page() -> None:
    css = _css()
    block = css[css.index(".toasts {") : css.index(".toasts {") + 700]

    assert "pointer-events: none;" in block, (
        "a centred stack over the page must not swallow clicks meant for the page"
    )

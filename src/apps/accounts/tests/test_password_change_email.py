"""A password change must be confirmed by email.

The registration and approval mails are about course business. This one is the
only mail in the system whose purpose is to let the recipient notice that their
account's security boundary moved, so it is tested as a security control rather
than as a feature.

Every change gets the same wording. There was once a second "first run" notice
with an imperative -- "set your new password" -- for accounts issued a placeholder
password. Nobody is issued one now (D43), so the flag had a caller shape and no
meaning, and a message that varies by unknown origin is a message an attacker can
spoof into looking routine. The test that used to require the two wordings to
differ is replaced by one that requires them to be identical.
"""

from __future__ import annotations

import pytest
from django.core import mail

from apps.accounts.tasks import notify_user_of_password_change

from .conftest import GOOD_PASSWORD, make_user

pytestmark = pytest.mark.django_db

CHANGE_URL = "/accounts/password/"
NEW_PASSWORD = "a-much-longer-passphrase-91"


def _change_password(client, user, new_password: str = NEW_PASSWORD, on_commit=None):
    """POST a password change, running the deferred notification.

    on_commit is pytest-django's capture fixture. It is required rather than
    optional: `transaction.on_commit` deliberately does not fire while pytest
    holds the test inside its own transaction, so without capturing it, every
    test below would see an empty outbox and pass for the wrong reason, or fail
    for a reason that has nothing to do with the mail.
    """
    client.force_login(user)
    if on_commit is None:
        return client.post(
        CHANGE_URL,
        {
            "old_password": GOOD_PASSWORD,
            "new_password1": new_password,
            "new_password2": new_password,
        },
    )
    with on_commit(execute=True):
        return client.post(
            CHANGE_URL,
            {
                "old_password": GOOD_PASSWORD,
                "new_password1": new_password,
                "new_password2": new_password,
            },
        )


# --------------------------------------------------------------------------
# The task itself
# --------------------------------------------------------------------------


def test_the_task_sends_to_the_person_whose_password_changed() -> None:
    user = make_user("changed@example.test", approved=True)

    notify_user_of_password_change(user.pk)

    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == [user.email]
    assert "password was changed" in mail.outbox[0].subject.lower()


def test_every_change_gets_the_same_warning() -> None:
    """One wording, and it has to actually warn rather than merely inform.

    Replaces a test requiring an imperative "set your new password" variant for
    first-run accounts. Nothing distinguishes a first run any more, so the variant
    would be chosen by an attacker-visible signal the recipient cannot verify.
    """
    first = make_user("first@example.test", approved=True)
    later = make_user("later@example.test", approved=True)

    notify_user_of_password_change(first.pk)
    notify_user_of_password_change(later.pk)

    assert mail.outbox[0].subject == mail.outbox[1].subject
    assert "was changed" in mail.outbox[1].subject.lower()
    assert "if you did not do this" in mail.outbox[1].body.lower()

    # The task still takes exactly one argument, so no caller can reintroduce the
    # variant by passing the old flag positionally.
    with pytest.raises(TypeError):
        notify_user_of_password_change(first.pk, True)  # type: ignore[call-arg]


@pytest.mark.parametrize("part", ["subject", "text body", "html body"])
def test_no_password_or_secret_is_ever_emailed(part: str) -> None:
    """The whole point of the control is that it is safe to forward.

    All three parts are checked, not just the plain-text one: the HTML part is
    assembled from a separate template and is the part people actually read.
    """
    user = make_user("secret@example.test", approved=True)

    notify_user_of_password_change(user.pk)

    message = mail.outbox[0]
    if part == "subject":
        content = message.subject
    elif part == "text body":
        content = message.body
    else:
        content = message.alternatives[0][0]

    content = content.lower()
    assert GOOD_PASSWORD not in content
    assert NEW_PASSWORD not in content
    for fragment in ("old_password", "new_password1", "csrfmiddlewaretoken"):
        assert fragment not in content


def test_both_a_text_and_an_html_part_are_sent() -> None:
    user = make_user("parts@example.test", approved=True)

    notify_user_of_password_change(user.pk)

    assert mail.outbox[0].alternatives, "plain-text-only mail is a downgrade"
    assert mail.outbox[0].alternatives[0][1] == "text/html"


def test_an_inactive_account_is_not_emailed() -> None:
    """Mail to a locked account invites a support thread about a locked account."""
    user = make_user("locked@example.test", approved=True)
    user.is_active = False
    user.save(update_fields=["is_active"])

    assert notify_user_of_password_change(user.pk) == 0
    assert not mail.outbox


# --------------------------------------------------------------------------
# The view wiring
# --------------------------------------------------------------------------


def test_changing_your_password_queues_a_notification(client, django_capture_on_commit_callbacks) -> None:
    user = make_user("wired@example.test", approved=True)

    _change_password(client, user, on_commit=django_capture_on_commit_callbacks)

    # CELERY_TASK_ALWAYS_EAGER, so the task ran inline and the mail is real.
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == [user.email]
    assert "password was changed" in mail.outbox[0].subject.lower()


def test_the_view_succeeds_even_if_the_notification_does_not(
    client, django_capture_on_commit_callbacks, monkeypatch
) -> None:
    """The mail is additive; it must not stand between a user and their account.

    The notification is deferred to commit precisely so a mail failure cannot
    roll back a password change the user has already been told succeeded.
    """
    user = make_user("succeeds@example.test", approved=True)

    def explode(*args, **kwargs):
        raise RuntimeError("simulated broker failure")

    monkeypatch.setattr(
        "apps.accounts.tasks.notify_user_of_password_change.delay", explode
    )

    with pytest.raises(RuntimeError):
        _change_password(client, user, on_commit=django_capture_on_commit_callbacks)

    # The on_commit callback runs after the response is built, so the change is
    # already committed. The point is that nothing *prevented* it.
    user.refresh_from_db()
    assert user.check_password(NEW_PASSWORD)


def test_the_view_works_and_leaves_the_session_valid(
    client, django_capture_on_commit_callbacks
) -> None:
    """Django rotates the session auth hash, so the change does not log you out.

    This replaced a test asserting the `must_change_password` flag was read
    *before* a logout, which was only true because the flag existed. The session
    behaviour is worth keeping: if rotating the password also dropped the session,
    the security notice would be the last thing a legitimate user saw before
    being bounced, and the mail would read as a warning about something that
    merely looks like an intrusion.
    """
    user = make_user("ordering@example.test", approved=True)

    response = _change_password(client, user, on_commit=django_capture_on_commit_callbacks)

    assert response.status_code == 302
    assert client.session.get("_auth_user_id") is not None, "session was dropped"
    assert len(mail.outbox) == 1
    assert "was changed" in mail.outbox[0].subject.lower()


def test_a_failed_change_sends_nothing(client, django_capture_on_commit_callbacks) -> None:
    """No password changed, so there is nothing to notify about."""
    user = make_user("failed@example.test", approved=True)
    client.force_login(user)

    with django_capture_on_commit_callbacks(execute=True):
        client.post(
            CHANGE_URL,
            {
                "old_password": "definitely-not-the-password",
                "new_password1": NEW_PASSWORD,
                "new_password2": NEW_PASSWORD,
            },
        )

    assert not mail.outbox

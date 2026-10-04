"""A password change must be confirmed by email.

The registration and approval mails are about course business. This one is the
only mail in the system whose purpose is to let the recipient notice that their
account's security boundary moved, so it is tested as a security control rather
than as a feature.
"""

from __future__ import annotations

import pytest
from django.core import mail
from django.urls import reverse

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

    notify_user_of_password_change(user.pk, False)

    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == [user.email]
    assert "password was changed" in mail.outbox[0].subject.lower()


def test_the_first_run_wording_differs_from_an_ordinary_change() -> None:
    """One is an instruction, one is a warning. They must not read the same."""
    first = make_user("first@example.test", approved=True)
    later = make_user("later@example.test", approved=True)

    notify_user_of_password_change(first.pk, True)
    notify_user_of_password_change(later.pk, False)

    first_subject = mail.outbox[0].subject
    later_subject = mail.outbox[1].subject

    assert first_subject != later_subject
    assert "set your new password" in first_subject.lower()
    assert "was changed" in later_subject.lower()

    # The security notice has to actually warn, not merely inform.
    assert "if you did not do this" in mail.outbox[1].body.lower()


@pytest.mark.parametrize("part", ["subject", "text body", "html body"])
def test_no_password_or_secret_is_ever_emailed(part: str) -> None:
    """The whole point of the control is that it is safe to forward.

    All three parts are checked, not just the plain-text one: the HTML part is
    assembled from a separate template and is the part people actually read.
    """
    user = make_user("secret@example.test", approved=True)

    notify_user_of_password_change(user.pk, False)

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

    notify_user_of_password_change(user.pk, False)

    assert mail.outbox[0].alternatives, "plain-text-only mail is a downgrade"
    assert mail.outbox[0].alternatives[0][1] == "text/html"


def test_an_inactive_account_is_not_emailed() -> None:
    """Mail to a locked account invites a support thread about a locked account."""
    user = make_user("locked@example.test", approved=True)
    user.is_active = False
    user.save(update_fields=["is_active"])

    assert notify_user_of_password_change(user.pk, False) == 0
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


def test_the_view_still_succeeds_and_clears_the_forced_flag(client, django_capture_on_commit_callbacks) -> None:
    """The mail is additive; it must not stand between a user and their account."""
    user = make_user("clears@example.test", approved=True)
    user.must_change_password = True
    user.save(update_fields=["must_change_password"])

    response = _change_password(client, user)

    assert response.status_code == 302
    user.refresh_from_db()
    assert user.must_change_password is False


def test_the_first_run_flag_is_read_before_the_user_is_logged_out(client, django_capture_on_commit_callbacks) -> None:
    """Regression guard on ordering.

    PasswordChangeView logs the user out inside super().form_valid(). Reading
    must_change_password afterwards would always see False, and every bootstrap
    password would be reported as an ordinary change.
    """
    user = make_user("ordering@example.test", approved=True)
    user.must_change_password = True
    user.save(update_fields=["must_change_password"])

    _change_password(client, user, on_commit=django_capture_on_commit_callbacks)

    assert len(mail.outbox) == 1
    assert "set your new password" in mail.outbox[0].subject.lower()


def test_an_ordinary_change_is_not_reported_as_a_first_run(client, django_capture_on_commit_callbacks) -> None:
    user = make_user("ordinary@example.test", approved=True)

    _change_password(client, user, on_commit=django_capture_on_commit_callbacks)

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

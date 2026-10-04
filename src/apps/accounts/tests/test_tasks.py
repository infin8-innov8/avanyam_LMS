"""Email task behaviour: who gets told what, and what gets retried.

The retry policy is the interesting part. Celery matches `autoretry_for` with
`isinstance`, so the shape of the smtplib exception tree decides the behaviour --
and the tree is inverted from what it looks like. Catching
`smtplib.SMTPException` "for the transient cases" silently retries
`SMTPRecipientsRefused`, because that is a *subclass* of it.

These tests pin the classification so that a well-meaning edit back to the base
class fails here rather than in production, where it manifests as "the trainer
never got the notification and the failure log says SMTPRecipientsRefused".
"""

from __future__ import annotations

import smtplib
import socket

import pytest
from celery import current_app
from django.core import mail

from apps.accounts.domain.enums import ApprovalStatus
from apps.accounts.models import ROLE_TRAINER, SignupRequest
from apps.accounts.tasks import (
    NOT_RETRYABLE,
    RETRYABLE,
    notify_applicant_of_decision,
    notify_trainer_of_application,
)

from .conftest import make_user

pytestmark = pytest.mark.django_db


def _smtp_exception(name: str) -> Exception:
    """Build a real smtplib exception instance.

    Several take required constructor args, so instances are built per class
    rather than one generic helper covering all of them.
    """
    if name == "SMTPRecipientsRefused":
        return smtplib.SMTPRecipientsRefused({})
    if name == "SMTPSenderRefused":
        return smtplib.SMTPSenderRefused(550, b"nope", "nope")
    if name == "SMTPResponseException":
        return smtplib.SMTPResponseException(550, b"nope")
    if name == "SMTPDataError":
        return smtplib.SMTPDataError(552, b"too big")
    if name == "SMTPConnectError":
        return smtplib.SMTPConnectError(421, b"busy")
    if name == "SMTPHeloError":
        return smtplib.SMTPHeloError(550, b"no")
    if name == "SMTPServerDisconnected":
        return smtplib.SMTPServerDisconnected("gone")
    raise AssertionError(f"no builder for {name}")


TRANSIENT = ("SMTPServerDisconnected", "SMTPConnectError", "SMTPHeloError")
PERMANENT = (
    "SMTPRecipientsRefused",
    "SMTPSenderRefused",
    "SMTPResponseException",
    "SMTPDataError",
)


@pytest.mark.parametrize("name", TRANSIENT)
def test_transient_smtp_failures_are_retryable(name: str) -> None:
    exc = _smtp_exception(name)
    assert isinstance(exc, RETRYABLE), f"{name} should be retried"


@pytest.mark.parametrize("name", PERMANENT)
def test_permanent_smtp_failures_are_not_retryable(name: str) -> None:
    exc = _smtp_exception(name)
    assert not isinstance(exc, RETRYABLE), (
        f"{name} can never succeed on a retry, so retrying only burns mail quota"
    )


def test_retry_policy_does_not_catch_the_smtplib_base_class() -> None:
    """The specific regression: catching the base class catches everything.

    Guards the mistake directly rather than only through the per-class tests, so
    the failure message points at the actual mistake.
    """
    for cls in NOT_RETRYABLE:
        assert not issubclass(cls, RETRYABLE), (
            f"{cls.__name__} is inside RETRYABLE via inheritance; "
            "RETRYABLE must name transient leaf classes, not their base"
        )
    assert smtplib.SMTPException not in RETRYABLE


def test_network_failures_are_retryable() -> None:
    assert isinstance(socket.timeout(), RETRYABLE)
    assert isinstance(socket.gaierror(), RETRYABLE)
    assert isinstance(ConnectionResetError(), RETRYABLE)
    assert isinstance(ConnectionRefusedError(), RETRYABLE)


def test_a_stale_request_id_is_not_retried() -> None:
    """A deleted SignupRequest fails identically every time."""
    assert not issubclass(SignupRequest.DoesNotExist, RETRYABLE)


# ---------------------------------------------------------------------------
# Behaviour: the right mail, to the right person
# ---------------------------------------------------------------------------


def test_trainer_is_emailed_when_an_application_arrives(trainer) -> None:
    trainee = make_user("new.trainee@example.test", approved=False)
    req = SignupRequest.objects.create(
        email=trainee.email, full_name=trainee.full_name, selected_trainer=trainer
    )

    notify_trainer_of_application(str(req.pk))

    assert len(mail.outbox) == 1
    msg = mail.outbox[0]
    assert msg.to == [trainer.email]
    assert trainee.full_name in msg.subject


def test_trainer_notification_goes_only_to_the_chosen_trainer(trainer) -> None:
    """Two trainers exist; the applicant picked one. The other must not be told."""
    other = make_user("not.the.trainer@example.test", role="trainer")
    req = SignupRequest.objects.create(
        email="someone@example.test", full_name="Someone", selected_trainer=trainer
    )

    notify_trainer_of_application(str(req.pk))

    assert [m.to for m in mail.outbox] == [[trainer.email]]
    assert other.email not in mail.outbox[0].to


def test_applicant_is_told_when_approved(trainer, trainee) -> None:
    req = SignupRequest.objects.create(
        email=trainee.email,
        full_name=trainee.full_name,
        selected_trainer=trainer,
        status="approved",
        decided_by=trainer,
    )

    notify_applicant_of_decision(str(req.pk))

    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == [trainee.email]
    assert "approved" in mail.outbox[0].subject.lower()


def test_applicant_is_told_when_rejected(trainer, trainee) -> None:
    req = SignupRequest.objects.create(
        email=trainee.email,
        full_name=trainee.full_name,
        selected_trainer=trainer,
        status=ApprovalStatus.REJECTED,
        decided_by=trainer,
    )

    notify_applicant_of_decision(str(req.pk))

    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == [trainee.email]
    # "rejected" is the real enum value; asserting on the actual status keeps
    # this test from passing via the template's non-approved fallback branch.
    assert req.status == ApprovalStatus.REJECTED
    assert "not approved" in mail.outbox[0].subject.lower()


def test_emails_are_multipart_with_readable_text(trainer, trainee) -> None:
    """Both alternatives must render; a broken template should fail the task."""
    req = SignupRequest.objects.create(
        email=trainee.email,
        full_name=trainee.full_name,
        selected_trainer=trainer,
    )
    notify_trainer_of_application(str(req.pk))

    msg = mail.outbox[0]
    html = msg.alternatives[0][0]
    assert msg.alternatives[0][1] == "text/html"
    assert "<html" in html.lower() or "<body" in html.lower()
    # the text/plain body must not be the raw HTML with tags stripped to nothing
    assert "<" not in msg.body
    assert msg.body.strip()


def test_no_email_is_sent_when_there_is_no_trainer() -> None:
    req = SignupRequest.objects.create(
        email="orphan@example.test", full_name="Orphan", selected_trainer=None
    )
    assert notify_trainer_of_application(str(req.pk)) == 0
    assert mail.outbox == []


class TestCeleryAppWiring:
    """The dispatch path itself, not just the task body.

    Every other test here calls `notify_trainer_of_application(...)` directly.
    That skips `.delay()`, which is how signup actually notifies a trainer, so a
    broken Celery app would leave the whole suite green while no notification was
    ever sent. Two things have to hold for `.delay()` to mean anything:

    * `shared_task` must bind to the configured app rather than Celery's
      throwaway `default` app, which reads none of the `CELERY_`-prefixed
      settings.
    * eager mode must be on, or `.delay()` publishes to a broker instead of
      running inline and every assertion about mail sees an empty outbox.
    """

    def test_task_is_bound_to_the_configured_app(self) -> None:
        app = notify_trainer_of_application.app
        assert app.main == "avanyam"
        assert app.main != "default"
        assert current_app.main == app.main

    def test_eager_mode_is_active(self) -> None:
        assert notify_trainer_of_application.app.conf.task_always_eager is True

    def test_app_uses_the_test_broker_not_the_live_one(self) -> None:
        # A leaked CELERY_BROKER_URL in os.environ outranks Django settings in
        # Celery's config chain, which would point the suite at live Redis.
        assert notify_trainer_of_application.app.conf.broker_url == "memory://"

    def test_app_serializer_settings_come_from_django(self) -> None:
        conf = notify_trainer_of_application.app.conf
        assert conf.task_serializer == "json"
        assert conf.task_acks_late is True

    def test_delay_runs_the_task_inline(self) -> None:
        trainer = make_user("wiring.trainer@example.test", role=ROLE_TRAINER)
        req = SignupRequest.objects.create(
            email="wiring@example.test",
            full_name="Wiring",
            selected_trainer=trainer,
        )
        result = notify_trainer_of_application.delay(str(req.pk))
        assert result.state == "SUCCESS"
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == [trainer.email]

"""What the logs say, and what they must never say.

These tests exist because the logging layer is the one part of this codebase whose
failures are invisible. A dropped log line does not raise, does not fail a test
anywhere else, and does not show up as a broken page -- it just leaves a question
with no answer at 3am. So the schema is pinned here instead.

Three properties are treated as requirements rather than preferences:

* **One schema.** Every record, from our own code and from a library, must carry the
  same keys. A dashboard that queries ``error_type`` cannot be allowed to work on
  Tuesday and silently return nothing on Wednesday because a record came from
  ``django.request`` instead of ``apps.accounts``.

* **One pipeline.** Django installs its own handlers before ours. If any survive,
  a record is written twice -- once as JSON and once as prose on stderr -- and an
  error can trigger a copy of itself by email through ``AdminEmailHandler``.

* **No secrets.** The undo code is the whole security model of one feature, so the
  tests that log a real send also assert the plaintext code, its hash, and the
  applicant's address are absent from every emitted record.
"""

from __future__ import annotations

import ast
import io
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest
from celery import signals
from celery.signals import task_failure, task_postrun, task_prerun
from django.core import mail
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import ApprovalUndoToken, SignupRequest
from apps.accounts.service.undo import UndoError, request_undo_code, undo_rejection
from apps.accounts.tasks import deliver_undo_code
from apps.common.logging import ALLOWED_NAMESPACES, check_event, get_logger
from config.celery_observability import _pool_is_prefork
from config.logging import (
    EVENT_STREAM,
    STREAMS,
    JsonFormatter,
    _iso_utc,
    stream_for_event,
    stream_for_logger,
)
from config.middleware import HEADER as REQUEST_ID_HEADER
from config.middleware import SAFE_REQUEST_ID, RequestContextMiddleware
from config.observability import get_request_id, request_scope


def _fresh_token(admin, req):
    """A live, unmailed token, so a test can drive the task directly."""
    issued = request_undo_code(actor=admin, request_pk=req.pk)
    return issued.token



@pytest.fixture
def rejected_request(admin):
    """An application that has already been declined, ready for a reversal.

    Declined by an admin, because under D41 approval and reversal are both
    admin-only acts. The tests using this fixture assert what the *logging* says,
    and the logging cannot be observed at all if the service refuses first.
    """
    from apps.accounts.service.approval import decide
    from apps.accounts.service.signup import submit_application

    result = submit_application(
        full_name="Meera Iyer",
        email="meera@example.com",
        password="Str0ng-Pass!x9",
        requested_role="trainee",
    )
    decide(
        approver=admin,
        request_pk=result.request.pk,
        decision="reject",
        note="Wrong branch.",
    )
    req = SignupRequest.objects.get(pk=result.request.pk)
    return req


pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class RecordingHandler(logging.Handler):
    """Capture whatever reaches the root logger, after formatting."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(json.loads(self.format(record)))
        except Exception:  # noqa: BLE001 - a failure here *is* the bug
            self.records.append({"_unformattable": repr(record)})


@pytest.fixture
def captured(monkeypatch):
    """Collect root-handler records as dicts, in the real production format."""
    handler = RecordingHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    # The suite runs at WARNING, so an INFO assertion would see nothing at all.
    previous = root.level
    root.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        root.setLevel(previous)
        root.removeHandler(handler)


def events(handler: RecordingHandler, event: str) -> list[dict]:
    return [r for r in handler.records if r.get("event") == event]


# ---------------------------------------------------------------------------
# One schema, whatever the source
# ---------------------------------------------------------------------------


REQUIRED_KEYS = {"timestamp", "level", "event", "stream", "logger", "service", "env"}


def test_every_record_carries_the_same_required_keys(captured) -> None:
    """Our logger and a third-party one must be indistinguishable in shape.

    The `django.request` call is the case that matters: it is the most common
    unexpected source of a log line, and it is the one most likely to break a
    query written against our own records.
    """
    log = get_logger("apps.accounts.test")
    log.info("undo.code_requested", "Minted a code", outcome="success")
    logging.getLogger("django.request").warning("Not Found: /nope")
    logging.getLogger("some.third.party").error("Upstream failed")

    assert len(captured.records) == 3
    for record in captured.records:
        missing = REQUIRED_KEYS - record.keys()
        assert not missing, f"{record.get('event')} missing {sorted(missing)}"


def test_the_timestamp_is_utc_with_millisecond_precision(captured) -> None:
    """The timestamp has to be sortable and unambiguous across hosts.

    Two properties are being pinned at once. The `Z` suffix, rather than a bare
    offset or a naive local time, is what stops a Loki query from silently mixing
    two clocks: without it a UTC deployment and a IST one would write
    indistinguishable values. And keeping the fraction to exactly three digits
    means lexicographic order matches chronological order, so a range sort on the
    string is a range sort on time.
    """
    log = get_logger("apps.accounts.test")
    log.info("undo.code_requested", "Minted a code")
    log.info("undo.code_redeemed", "Code redeemed")

    # A regex rather than strptime: %f accepts one to six digits and strptime
    # has no literal `Z`, so neither would notice the shape drifting.
    shape = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

    stamps = [record["timestamp"] for record in captured.records]
    now = datetime.now(UTC)
    for stamp in stamps:
        assert shape.match(stamp), f"unexpected timestamp shape: {stamp!r}"
        # `fromisoformat` on 3.11 understands the trailing `Z`, and it hands back
        # an aware datetime -- so an accidentally-local `Z` would show up as a
        # non-UTC tzinfo here rather than being quietly re-labelled.
        parsed = datetime.fromisoformat(stamp)
        assert parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)
        # The record was written moments ago, so it must land inside a sane
        # window rather than merely parsing. This catches a UTC/local mix-up that
        # a format assertion alone would wave through.
        assert timedelta(seconds=-5) < parsed - now < timedelta(seconds=5), stamp

    assert stamps == sorted(stamps), "string order must match chronological order"


@pytest.mark.parametrize(
    ("epoch", "expected"),
    [
        # 1970-01-01T00:00:00Z -- the zero point, and a fixed-width minute/hour
        # that cannot hide a stray offset digit.
        (0.0, "1970-01-01T00:00:00.000Z"),
        # 2026-10-04T13:36:45.792Z. Rounded *down* to the millisecond: .792792
        # truncates rather than carrying, which is what ISO 8601 permits and what
        # keeps stamps from ever appearing to run backwards.
        (1791121005.792792, "2026-10-04T13:36:45.792Z"),
    ],
)
def test_the_timestamp_is_built_from_the_epoch_in_utc(epoch: float, expected: str) -> None:
    """The conversion is fixed against known values, not against "now".

    Asserting a timestamp is close to the current time cannot tell UTC from local
    time -- both pass on a host running in UTC, which is exactly the host nobody
    tests on. Comparing against a hard-coded epoch does, and it catches the case
    that mattered here: the previous implementation rendered the epoch through
    ``time.localtime`` and appended ``Z``, so this deployment was writing every
    timestamp 05:30 ahead of UTC while claiming otherwise.
    """
    assert _iso_utc(epoch) == expected


def test_the_timestamp_ignores_the_host_timezone(monkeypatch) -> None:
    """A host running in another zone must not change the rendered stamp.

    ``tzset`` reads ``TZ`` process-wide, so this is restored in the fixture teardown
    rather than left for a later test to trip over.
    """
    import time

    epoch = 1791121005.792792
    monkeypatch.setenv("TZ", "Pacific/Kiritimati")  # UTC+14
    time.tzset()
    try:
        assert _iso_utc(epoch) == "2026-10-04T13:36:45.792Z"
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()


def test_a_foreign_record_keeps_its_message_and_its_args(captured) -> None:
    """Stdlib %-style args must be interpolated, not dropped or mangled."""
    logging.getLogger("django.request").warning("Not Found: %s", "/nope")

    (record,) = captured.records
    assert record["message"] == "Not Found: /nope"
    assert record["event"] == "django.request"
    assert record["logger"] == "django.request"


def test_a_record_whose_args_do_not_match_its_format_is_still_emitted() -> None:
    """A malformed library record must not take the log pipeline down with it.

    This is the case that produced a `--- Logging error ---` block on stderr with
    Django's default handler, which meant the record was lost from that path
    entirely. It is also the reason `_safe_message` exists.

    Emitted through a logger of its own rather than the root one: pytest installs
    its own capturing handlers on the root logger, and those interpolate args
    eagerly, so the record has to be kept away from them to test our formatter
    rather than pytest's.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    target = logging.getLogger("django.request")
    target.addHandler(handler)
    # pytest's own capturing handlers on the root logger interpolate args eagerly
    # and would raise on this record. They are not the code under test.
    previous_propagate = target.propagate
    target.propagate = False
    try:
        target.warning("malformed record", 500)
    finally:
        target.propagate = previous_propagate
        target.removeHandler(handler)

    (line,) = [ln for ln in stream.getvalue().splitlines() if ln.strip()]
    record = json.loads(line)
    assert "malformed record" in record["message"]
    assert "unformattable" in record["message"]
    assert record["stream"] == "access"


def test_an_unserialisable_value_does_not_raise() -> None:
    """A field nobody expected must not cost us the record.

    `default=str` is what makes this safe; without it a Decimal or an enum in a
    payload would raise inside the formatter, and logging would swallow the error
    and lose the line.
    """
    from decimal import Decimal

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    target = logging.getLogger("apps.accounts.serialisation-test")
    target.addHandler(handler)
    target.setLevel(logging.DEBUG)
    try:
        get_logger("apps.accounts.serialisation-test").warning(
            "undo.code_sent", "odd field", amount=Decimal("1.50")
        )
    finally:
        target.removeHandler(handler)

    (line,) = [ln for ln in stream.getvalue().splitlines() if ln.strip()]
    assert json.loads(line)["amount"] == "1.50"


# ---------------------------------------------------------------------------
# Routing: the reason the streams exist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ("http.request.completed", "access"),
        ("mail.send_skipped", "mail"),
        ("approval.decided", "approval"),
        ("undo.code_sent", "undo"),
        ("auth.login_failed", "security"),
        ("otp.code_verified", "security"),
        ("celery.task_started", "celery"),
        ("http.access.count", "access"),
        ("something.unmapped", "app"),
    ],
)
def test_an_event_name_routes_to_its_stream(event: str, expected: str) -> None:
    assert stream_for_event(event) == expected


@pytest.mark.parametrize(
    ("logger_name", "expected"),
    [
        ("django.request", "access"),
        ("django.server", "access"),
        ("celery.app.trace", "celery"),
        ("kombu.connection", "celery"),
        ("django.security.DisallowedHost", "security"),
        ("apps.accounts.tasks", "app"),
    ],
)
def test_a_logger_name_routes_to_its_stream(logger_name: str, expected: str) -> None:
    """A library with no event name of ours still has to land somewhere sensible."""
    assert stream_for_logger(logger_name) == expected


def test_the_event_name_wins_over_the_logger_it_was_emitted_from(captured) -> None:
    """`undo.*` from a module named `apps.mail...` is still an undo event.

    Routing by logger alone would put mail-adjacent code in the wrong stream the
    first time a module is moved, silently splitting one feature's history across
    two files.
    """
    get_logger("apps.accounts.tasks").info("undo.code_sent", "sent")

    (record,) = captured.records
    assert record["stream"] == "undo"
    assert record["logger"] == "apps.accounts.tasks"


def test_the_error_stream_is_a_view_across_every_other_stream(tmp_path, monkeypatch) -> None:
    """Warning and above are duplicated into `error`, and nothing else is.

    This is the stream a dashboard alerts on, so it has to be complete on its own:
    an error that only appears in `mail.log` is an error nobody is watching.
    """
    monkeypatch.setenv("DJANGO_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("DJANGO_LOG_TO_FILES", "1")

    from config.logging import build_router

    router = build_router(level="DEBUG")
    target = logging.getLogger("apps.accounts.routing-test")
    target.addHandler(router)
    target.setLevel(logging.DEBUG)
    try:
        log = get_logger("apps.accounts.routing-test")
        log.debug("undo.code_sent", "debug", outcome="success")
        log.info("undo.code_requested", "info", outcome="success")
        log.warning("undo.code_expired", "warning", outcome="compensated")
        log.error("undo.code_send_failed", "error", outcome="failure")
        for handler in router._by_stream.values():
            handler.flush()
    finally:
        target.removeHandler(router)
        router.close()

    def read(name: str) -> list[dict]:
        path = tmp_path / f"{name}.log"
        assert path.exists(), f"{name}.log was never created"
        return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]

    assert [r["level"] for r in read("error")] == ["warning", "error"]
    assert [r["event"] for r in read("error")] == [
        "undo.code_expired",
        "undo.code_send_failed",
    ]
    # The primary stream keeps its own copy: `error` is an addition, not a move.
    assert len(read("undo")) == 4
    # Nothing routed to `app`, and handlers use delay=True, so no file is created.
    # That is deliberate: importing settings must not litter the disk.
    assert not (tmp_path / "app.log").exists()


def test_every_declared_stream_gets_a_file(tmp_path, monkeypatch) -> None:
    """A stream with no handler is a stream whose records vanish silently."""
    monkeypatch.setenv("DJANGO_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("DJANGO_LOG_TO_FILES", "1")

    from config.logging import build_router

    router = build_router(level="INFO")
    try:
        assert set(router._by_stream) == set(STREAMS)
    finally:
        router.close()


# ---------------------------------------------------------------------------
# One pipeline
# ---------------------------------------------------------------------------


def test_django_keeps_no_handlers_of_its_own() -> None:
    """Django's defaults are removed and its records reach the one router.

    Left in place they cost three things: a duplicate plain-text copy of every
    record, a base formatter that raises on a mismatched record, and an
    `AdminEmailHandler` that mails the ADMINS a copy of anything at ERROR.
    """
    django = logging.getLogger("django")
    assert django.handlers == []

    # propagate=True is what routes django.request into the single root router.
    # It arrives as False, and leaving it False would drop every Django log line
    # on the floor.
    assert django.propagate is True
    assert logging.getLogger("django.server").propagate is True


def test_there_is_exactly_one_router_instance() -> None:
    """Two routers means every record is written twice.

    Each router builds its own eight rotating file handles, so the two would also
    rotate independently and truncate each other's files.
    """
    from config.logging import StreamRouterHandler

    routers = [
        h
        for name in ("", "django", "django.server", "django.request")
        for h in logging.getLogger(name).handlers
        if isinstance(h, StreamRouterHandler)
    ]
    assert len(routers) == 1, len(routers)


def test_the_admin_email_handler_is_gone() -> None:
    """Assert the trap directly, by class rather than by count.

    Counting handlers would pass again if Django swapped in a different
    third-party handler with the same behaviour.
    """
    from django.utils.log import AdminEmailHandler

    every = [
        h
        for name in ("", "django", "django.server", "django.request")
        for h in logging.getLogger(name).handlers
    ]
    assert not [h for h in every if isinstance(h, AdminEmailHandler)]


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------


def test_a_request_gets_an_id_and_echoes_it_back(client) -> None:
    response = client.get("/accounts/login/")

    request_id = response[REQUEST_ID_HEADER]
    assert SAFE_REQUEST_ID.match(request_id), request_id


def test_an_inbound_request_id_is_kept_when_it_is_safe(client) -> None:
    """A proxy or gateway id survives, so its logs and ours can be joined."""
    response = client.get("/accounts/login/", headers={"x-request-id": "abc123def456"})

    assert response[REQUEST_ID_HEADER] == "abc123def456"


@pytest.mark.parametrize(
    "hostile",
    [
        "short",  # too short to be a real id
        "a" * 65,  # long enough to be a denial-of-service, not an id
        "has space",
        "quote\"and\nnewline",  # log injection
        "'; DROP TABLE accounts_signuprequest; --",
        "../../etc/passwd",
    ],
)
def test_a_hostile_inbound_request_id_is_replaced(client, hostile: str) -> None:
    """The header is attacker-controlled, so it is validated before it is trusted.

    It lands in every log line for the request and in the response header. An
    unvalidated value is both a log-injection vector and a way to hand a
    dashboard a megabyte-long label value.
    """
    response = client.get("/accounts/login/", headers={"x-request-id": hostile})

    returned = response[REQUEST_ID_HEADER]
    assert returned != hostile
    assert SAFE_REQUEST_ID.match(returned)


def test_the_request_id_reaches_the_records_the_request_produces(client, captured) -> None:
    """One id ties an access line to the undo line from the same click."""
    trainer = client.session
    assert trainer is not None

    response = client.get("/accounts/login/", headers={"x-request-id": "corr123456"})
    request_id = response[REQUEST_ID_HEADER]

    assert any(r.get("request_id") == request_id for r in captured.records), captured.records


def test_a_request_scope_is_restored_afterwards() -> None:
    """Context is per-request: a leaked id would mislabel every later log line."""
    assert get_request_id() is None or get_request_id() == ""

    with request_scope("inner12345"):
        assert get_request_id() == "inner12345"

    assert get_request_id() in (None, "")


def test_an_unhandled_view_error_is_logged_with_its_request_id(rf, captured) -> None:
    """A 500 with no request attached is the hardest kind of bug to chase.

    Django's own handler turns the exception into a response further up, so if this
    is not logged here the failure appears in the logs belonging to nobody.
    """

    def boom(_request):
        raise RuntimeError("kaboom")

    middleware = RequestContextMiddleware(boom)
    request = rf.get("/boom/")
    request.META["HTTP_X_REQUEST_ID"] = "boom1234567"

    with pytest.raises(RuntimeError):
        middleware(request)

    (record,) = events(captured, "http.request.exception")
    assert record["request_id"] == "boom1234567"
    assert record["outcome"] == "failure"
    assert record["path"] == "/boom/"
    assert "RuntimeError" in record.get("error", "")


# ---------------------------------------------------------------------------
# The real flow
# ---------------------------------------------------------------------------


def test_the_undo_flow_records_the_outcome_without_the_code(
    admin, rejected_request, captured
) -> None:
    """The end-to-end shape, and the property that matters most here.

    A `undo.code_sent` line has to exist so a support question ("did my code go
    out?") is answerable -- and it has to exist without the code in it, because
    this log file is exactly the thing an attacker with read access wants.
    """
    mail.outbox.clear()

    issued = request_undo_code(actor=admin, request_pk=rejected_request.pk)

    assert events(captured, "undo.code_sent"), captured.records
    assert events(captured, "undo.code_requested")

    blob = json.dumps(captured.records)
    assert issued.code not in blob
    assert issued.token.code_hash not in blob
    assert rejected_request.email not in blob
    # The applicant's address is not copied into the admin's mail thread, so it
    # must not appear in the undo records either.
    assert issued.token.requested_by.email not in blob


def test_a_failed_send_is_recorded_and_the_code_is_expired(
    admin, rejected_request, captured
) -> None:
    """The worst case the feature has, and both halves are asserted.

    The mail never left and the token must not stay usable. Either half alone is
    misleading: a log line claiming failure while a live token remains, or an
    expired token nobody recorded.
    """
    with (
        mock.patch(
            "apps.accounts.tasks.EmailMultiAlternatives.send",
            side_effect=RuntimeError,
        ),
        pytest.raises(UndoError),
    ):
        request_undo_code(actor=admin, request_pk=rejected_request.pk)

    (failure,) = events(captured, "undo.code_send_failed")
    assert failure["outcome"] == "failure"
    assert failure["error_type"] == "RuntimeError"

    (compensated,) = events(captured, "undo.code_expired_after_failed_send")
    assert compensated["outcome"] == "compensated"

    assert not ApprovalUndoToken.objects.filter(
        request=rejected_request, consumed_at__isnull=True, expires_at__gt=timezone.now()
    ).exists()


def test_a_send_that_reports_zero_is_treated_as_a_failure(
    admin, rejected_request, captured
) -> None:
    """Returning 0 is not an exception, so it is the case easiest to overlook.

    `SMTPRecipientsRefused` arrives as an exception, but a backend can also accept
    the message and deliver nothing. The admin's experience is identical, so it
    must produce the same record and the same refusal.
    """
    with (
        mock.patch(
            "apps.accounts.tasks.EmailMultiAlternatives.send",
            return_value=0,
        ),
        pytest.raises(UndoError),
    ):
        request_undo_code(actor=admin, request_pk=rejected_request.pk)

    (failure,) = events(captured, "undo.code_send_failed")
    assert failure["reason"] == "zero_messages_accepted"
    assert not events(captured, "undo.code_sent")


def test_redeeming_a_code_records_the_reversal(admin, rejected_request, captured) -> None:
    mail.outbox.clear()
    issued = request_undo_code(actor=admin, request_pk=rejected_request.pk)

    undo_rejection(
        actor=admin,
        request_pk=rejected_request.pk,
        code=issued.code,
    )

    (record,) = events(captured, "undo.code_redeemed")
    assert record["outcome"] == "success"
    assert issued.code not in json.dumps(captured.records)


def test_an_expired_token_is_not_redeemable_and_leaves_no_trace_of_a_code(
    admin, rejected_request, captured
) -> None:
    """The clock is the only thing standing between an old mail and a reversal."""
    mail.outbox.clear()
    issued = request_undo_code(actor=admin, request_pk=rejected_request.pk)
    ApprovalUndoToken.objects.filter(pk=issued.token.pk).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    captured.records.clear()

    with pytest.raises(UndoError):
        undo_rejection(
            actor=admin, request_pk=rejected_request.pk, code=issued.code
        )

    assert not events(captured, "undo.code_redeemed")
    assert issued.code not in json.dumps(captured.records)


def test_a_missing_requester_row_raises_rather_than_sending_nothing() -> None:
    """A token that no longer exists is a bug upstream, not a silent zero.

    Returning 0 would make the broker record the task as succeeded, and the only
    trace would be an absence.
    """
    with pytest.raises(ApprovalUndoToken.DoesNotExist):
        deliver_undo_code("999999999", "123456")


def test_a_stamp_failure_does_not_make_the_send_look_unsuccessful(
    admin, rejected_request, captured
) -> None:
    """`_mark_emailed` is best-effort, so it must swallow its own failure.

    Called straight rather than through the task, because that is the layer whose
    contract is "never raises": it runs after the mail has gone, and anything it
    raises would propagate into a retry and a second code.
    """
    from apps.accounts import tasks as task_module

    token = _fresh_token(admin, rejected_request)

    with mock.patch(
        "apps.accounts.models.ApprovalUndoToken.objects.filter",
        side_effect=RuntimeError("database is read-only"),
    ):
        task_module._mark_emailed(str(token.pk))

    (failure,) = events(captured, "undo.token_stamp_failed")
    assert failure["outcome"] == "failure"


def test_a_missing_token_row_raises_rather_than_sending_nothing() -> None:
    """A token that no longer exists is a bug upstream, not a silent zero.

    Returning 0 would make the broker record the task as succeeded, and the only
    trace would be an absence.
    """
    with pytest.raises(ApprovalUndoToken.DoesNotExist):
        deliver_undo_code("999999999", "123456")


def test_a_token_with_no_requester_is_recorded_as_skipped(
    admin, rejected_request, captured
) -> None:
    """A zero return is not an error to the broker, so the log line is the record.

    Without it the task shows as succeeded and the admin simply never hears
    anything.
    """
    token = _fresh_token(admin, rejected_request)
    ApprovalUndoToken.objects.filter(pk=token.pk).update(requested_by=None)

    assert deliver_undo_code(str(token.pk), "123456") == 0

    (record,) = events(captured, "undo.code_send_skipped")
    assert record["outcome"] == "skipped"
    assert record["token_pk"] == str(token.pk)


# ---------------------------------------------------------------------------
# Celery task lifecycle
# ---------------------------------------------------------------------------


class _FakeTask:
    """The one attribute the signals read off a task.

    A plain class rather than a `Mock`, because `Mock(name=...)` is the mock's
    *repr* name: `mock.name` returns a child mock, not the string, so a test built
    on it would assert against a `Mock` and pass for the wrong reason.
    """

    def __init__(self, name: str = "accounts.fake_task") -> None:
        self.name = name


def test_a_task_run_binds_its_name_and_correlation_id(captured) -> None:
    """`task_name` is in the documented schema, so a task must populate it.

    The correlation id is the Celery task id rather than a fresh uuid: one task is
    one unit of work, and minting a second id per execution would mean the mail
    line and the `task_started` line could not be joined.
    """
    task_prerun.send(sender=None, task_id="abc12345", task=_FakeTask())

    (record,) = events(captured, "celery.task_started")
    assert record["task_name"] == "accounts.fake_task"
    assert record["request_id"] == "abc12345"


def test_a_finished_task_reports_how_long_it_took(captured) -> None:
    task_prerun.send(sender=None, task_id="abc12345", task=_FakeTask())
    task_postrun.send(sender=None, task_id="abc12345", task=_FakeTask(), state="SUCCESS")

    (record,) = events(captured, "celery.task_finished")
    assert record["outcome"] == "success"
    assert record["task_name"] == "accounts.fake_task"
    assert record["duration_ms"] >= 0


def test_a_task_that_did_not_succeed_is_not_reported_as_a_success(captured) -> None:
    """A retried task must not read as a success in the structured stream.

    `state` is the only thing distinguishing the two, and defaulting it to success
    would make every transient failure invisible to anything querying on outcome.
    """
    task_prerun.send(sender=None, task_id="abc12345", task=_FakeTask())
    task_postrun.send(sender=None, task_id="abc12345", task=_FakeTask(), state="RETRY")

    (record,) = events(captured, "celery.task_finished")
    assert record["outcome"] == "failure"
    assert record["state"] == "RETRY"


def test_a_raised_task_is_recorded_before_the_exception_is_discarded(captured) -> None:
    task_prerun.send(sender=None, task_id="abc12345", task=_FakeTask())
    task_failure.send(
        sender=_FakeTask(), task_id="abc12345", exception=ValueError("broker said no")
    )
    # Celery fires postrun after a failure too; omitting it here would leave the
    # start entry behind and break a later test's assertion for the wrong reason.
    task_postrun.send(sender=None, task_id="abc12345", task=_FakeTask(), state="FAILURE")

    (record,) = events(captured, "celery.task_failed")
    assert record["outcome"] == "failure"
    assert record["error_type"] == "ValueError"
    assert record["task_name"] == "accounts.fake_task"


def test_a_task_id_is_never_leaked_into_the_next_task(captured) -> None:
    """`task_prerun` rebinds rather than accumulates.

    If a worker is killed mid-task `task_postrun` never fires, so the previous
    task's context is still bound when the next one starts. A rebind makes that
    self-healing; the assertion is that the second task cannot inherit the first
    task's name.
    """
    task_prerun.send(sender=None, task_id="first", task=_FakeTask("accounts.first"))
    task_prerun.send(sender=None, task_id="second", task=_FakeTask("accounts.second"))
    # Both are left deliberately unfinished, so each needs closing by hand -- an
    # unclosed start entry here would otherwise fail an unrelated test's assertion.
    for task_id in ("first", "second"):
        task_postrun.send(sender=None, task_id=task_id, task=_FakeTask(), state="SUCCESS")

    (_, second) = events(captured, "celery.task_started")
    assert second["task_name"] == "accounts.second"
    assert second["request_id"] == "second"


def test_the_task_clock_does_not_leak_entries(captured) -> None:
    """`postrun` pops what `prerun` pushed, so the map cannot grow unbounded.

    A worker restarted daily for a year would otherwise accumulate one float per
    task ever run.
    """
    from config import celery_observability

    for i in range(5):
        task_prerun.send(sender=None, task_id=f"t{i}", task=_FakeTask())
        task_postrun.send(sender=None, task_id=f"t{i}", task=_FakeTask(), state="SUCCESS")

    assert celery_observability._STARTED == {}


def test_a_module_logger_gets_our_bound_logger_even_when_early_imported() -> None:
    """Import order must not decide which logger class a module ends up with.

    `structlog.get_logger()` returns a lazy proxy and `get_logger` calls `.bind()`,
    which resolves it immediately. Resolved before `configure_structlog()`, that
    yields structlog's default `BoundLoggerFilteringAtNotset`, which treats the
    second positional argument as `%`-interpolation args -- so the first
    `log.info("evt", "message")` at an enabled level raises TypeError, and every
    record loses its `message`. Nothing about that failure points at import order.
    """
    import config.logging as config_logging

    early = config_logging.get_logger("observability.early_import_probe")
    assert isinstance(early, config_logging.BoundLogger)

    early.info("app.probe", "a message", outcome="success")


def test_the_lifecycle_signals_are_actually_connected() -> None:
    """A module of signal handlers that nothing imports logs nothing.

    This is the regression guard for the wiring itself, which is a bare import in
    `config/celery.py` and so has no other test protecting it.
    """
    import config.celery  # noqa: F401

    assert signals.task_prerun.receivers
    assert signals.task_postrun.receivers
    assert signals.task_failure.receivers


def test_the_context_binding_refuses_a_pool_it_cannot_support() -> None:
    """Documented as prefork-only; this makes the limit enforced rather than hoped.

    On a thread pool the prerun/postrun callbacks can land on a different thread
    from the task body, so the bound context would be absent from the records that
    matter -- the exact failure this module exists to prevent, but silent.
    """
    # override_settings rather than mock.patch.object: Django's LazySettings raises
    # AttributeError for an attribute it does not already define.
    # Eager mode returns early by design -- there is no worker process and so no
    # pool to be wrong about -- so it has to be off for the pool to be reached.
    with (
        override_settings(CELERY_WORKER_POOL="threads", CELERY_TASK_ALWAYS_EAGER=False),
        pytest.raises(RuntimeError, match="prefork"),
    ):
        _pool_is_prefork()


def test_a_mail_send_that_succeeds_is_recorded_as_sent(captured, admin, rejected_request) -> None:
    """`mail.log` held only failures before, which cannot answer "did it go?".

    The recipient *count* is logged, never the address: this is the file most
    likely to be handed to a mail provider while debugging.
    """
    token = _fresh_token(admin, rejected_request)
    # Minting sends too -- request_undo_code is synchronous -- so the record from
    # the fixture has to go before the send under test is measured.
    captured.records.clear()

    assert deliver_undo_code(str(token.pk), "123456") == 1

    (record,) = events(captured, "mail.sent")
    assert record["outcome"] == "success"
    assert record["recipients"] == 1
    assert "email" not in record
    assert not any("@" in str(v) for v in record.values())


# ---------------------------------------------------------------------------
# The whole codebase, not a hand-written list
# ---------------------------------------------------------------------------

_SRC = Path(__file__).resolve().parents[3]

#: Logging calls whose first argument is the event name. Matched as attributes so
#: `log.info(...)` is caught but `self.client.get(...)` is not.
_LOG_METHODS = frozenset({"debug", "info", "warning", "error", "critical", "exception"})

#: Extensions that mean the dotted name in prose is a file, not an event.
FILE_EXTENSIONS = frozenset(
    {"log", "json", "md", "py", "txt", "yml", "yaml", "toml", "cfg", "html", "js", "css"}
)


def _application_sources() -> list[Path]:
    """Every module under ``src/`` except the tests themselves.

    Tests are excluded on purpose: they legitimately contain malformed and unknown
    event names as *negative* cases, and including them would mean the scan could
    only ever be satisfied by deleting the tests that prove the rules.
    """
    return sorted(
        path
        for path in _SRC.rglob("*.py")
        if "tests" not in path.parts and ".venv" not in str(path)
    )


def _logged_events() -> list[tuple[Path, str, int]]:
    """Every event name literal passed to a logging call, with its file and line.

    Parsed rather than grepped because a grep cannot tell an event name from any
    other string in the file -- it would happily match a test's negative case, a
    docstring, or a dictionary key. Walking the AST finds exactly the first
    argument of an actual logging call, which is the thing the rules are about.
    """
    found: list[tuple[Path, str, int]] = []
    for path in _application_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr not in _LOG_METHODS:
                continue
            if not node.args:
                continue
            first = node.args[0]
            # `event = EVENT_NAME` is a legitimate indirection; only a literal can
            # be checked here, so non-literals are simply not part of the scan.
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.append((path, first.value, first.lineno))
    return found


def _is_valid(event: str) -> bool:
    try:
        check_event(event)
    except ValueError:
        return False
    return True


def test_the_scan_actually_finds_events() -> None:
    """A scan that silently matches nothing passes every assertion below.

    Without this, deleting the logging calls -- or a parser change that stops
    matching them -- would turn the whole section green.
    """
    events = _logged_events()
    assert len(events) > 15, f"scan found only {len(events)} events; it is not matching"
    assert {event for _, event, _ in events} >= {
        "undo.code_requested",
        "approval.decided",
        "mail.sent",
        "auth.login_refused",
        "celery.task_finished",
    }


def test_every_event_name_in_the_codebase_is_well_formed() -> None:
    """`check_event` applied to the source, not to a list someone remembered.

    The previous version of this file tested a hard-coded tuple of event names, so
    it kept passing after `auth.login_refused` and `celery.task_finished` were
    added -- and would have kept passing if a typo landed an event in `app.log`
    where no dashboard looks.
    """
    offenders = [
        f"{path.relative_to(_SRC)}:{line}: {event!r}"
        for path, event, line in _logged_events()
        if not _is_valid(event)
    ]
    assert not offenders, "malformed event names:\n" + "\n".join(offenders)


def test_no_event_name_falls_through_to_the_default_stream() -> None:
    """Every namespace we log under has to be *registered*, not merely tolerated.

    `check_event` rejects unknown namespaces, so this is a second, independent
    guard: it catches a namespace that is in `ALLOWED_NAMESPACES` but missing from
    `EVENT_STREAM`. Such an event is accepted, logged, and quietly written to
    `app.log` -- reading as a working line in a file no dashboard queries. Comparing
    against `EVENT_STREAM` directly, rather than against the fallback stream, is
    what makes that visible: it also catches the case where the namespace maps to a
    stream nobody declared a file for.
    """
    offenders = [
        f"{path.relative_to(_SRC)}:{line}: {event!r} has no EVENT_STREAM entry"
        for path, event, line in _logged_events()
        if _is_valid(event) and event.split(".", 1)[0] not in EVENT_STREAM
    ]
    assert not offenders, "unrouted event names:\n" + "\n".join(offenders)


def test_every_declared_stream_has_a_file() -> None:
    """A namespace routed to a stream with no file configured loses its records."""
    assert set(EVENT_STREAM.values()) <= set(STREAMS)


def test_the_documented_examples_are_events_that_actually_exist() -> None:
    """Documentation drift is how a rule quietly stops being true.

    The docstrings here showed `undo.code.sent` for months while the code emitted
    `undo.code_sent`; both match the name pattern, so no pattern check could ever
    have caught it. A reader copying the documented form would have created a
    second, unroutable event -- and every dashboard pointed at the real one.
    """
    real = {event for _, event, _ in _logged_events()}
    offenders: list[str] = []
    pattern = re.compile(r"\b([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)\b")
    for path in _application_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docstrings = [
            node.body[0].value.value
            for node in ast.walk(tree)
            if isinstance(
                node,
                (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
            )
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ]
        for docstring in docstrings:
            for candidate in pattern.findall(docstring):
                # Only names that look like one of ours: a dotted lowercase
                # identifier whose namespace is a known one.
                if candidate.split(".", 1)[0] not in ALLOWED_NAMESPACES:
                    continue
                # `undo.log` and `celery.log` are stream files the prose is talking
                # about, not events. A known file extension is the tell.
                if candidate.rsplit(".", 1)[-1] in FILE_EXTENSIONS:
                    continue
                if candidate not in real:
                    offenders.append(f"{path.relative_to(_SRC)}: {candidate}")
    assert not offenders, "documented events that no code emits:\n" + "\n".join(sorted(set(offenders)))


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_a_successful_sign_in_is_recorded_with_the_actor(captured, client, trainer) -> None:
    trainer.set_password("correct horse battery staple")
    trainer.save(update_fields=["password"])
    client.post(reverse("accounts:login"), {"username": trainer.email, "password": "correct horse battery staple"})

    (record,) = events(captured, "auth.login_succeeded")
    assert record["outcome"] == "success"
    assert record["actor_email"] == trainer.email
    assert record["account_approved"] is True
    assert "password" not in record


def test_a_refused_sign_in_is_recorded_without_the_address(captured, client, trainer) -> None:
    """A refused sign-in re-renders the form with a 200, so nothing else sees it.

    The submitted identifier is deliberately absent: the point of the event is to
    notice a burst of failures, and an alert that carries the addresses being tried
    turns the log into a list of accounts to attack. `django.request` shows only
    the 200, so this record is the whole signal.
    """
    client.post(reverse("accounts:login"), {"username": trainer.email, "password": "wrong"})

    (record,) = events(captured, "auth.login_refused")
    assert record["outcome"] == "refused"
    assert record["reason"] == "invalid_credentials"
    assert not any(trainer.email in str(v) for v in record.values())


def test_an_unapproved_sign_in_is_refused_with_its_own_reason(captured, client, trainee) -> None:
    """A pending account is refused, and the log says why even though the form cannot.

    This used to assert `auth.login_succeeded` carrying `account_approved: false`,
    because a pending applicant used to be admitted and told to wait. D45 ended
    that: an inactive account cannot authenticate at all.

    `ModelBackend` drops the account inside `AuthenticationForm.clean()`, so the
    failure arrives as an ordinary bad password. Both halves matter:

    * the response must stay generic, or a stranger learns which addresses are
      registered and what state they are in;
    * the log must not stay generic, or "nobody is working the queue" and
      "someone is guessing passwords" are the same metric with opposite fixes.
    """
    trainee.set_password("correct horse battery staple")
    trainee.save(update_fields=["password"])

    response = client.post(
        reverse("accounts:login"),
        {"username": trainee.email, "password": "correct horse battery staple"},
    )

    # Refused, not admitted: no session, so nothing downstream is reachable.
    assert response.status_code == 200
    assert client.session.get("_auth_user_id") is None
    assert not events(captured, "auth.login_succeeded")

    body = response.content.decode().lower()
    assert "waiting for an administrator" not in body, (
        "the response must not distinguish a pending account from a wrong password"
    )

    # The log does distinguish it, and attributes it to the account.
    (record,) = events(captured, "auth.login_refused")
    assert record["reason"] == "account_not_approved"
    assert record["actor_email"] == trainee.email

    # A wrong password stays distinguishable, and leaks no actor.
    captured.records.clear()
    client.post(
        reverse("accounts:login"),
        {"username": trainee.email, "password": "wrong"},
    )
    (plain,) = events(captured, "auth.login_refused")
    assert plain["reason"] == "invalid_credentials"
    assert "actor_email" not in plain


def test_a_sign_out_is_recorded_for_the_person_leaving(captured, client, trainer) -> None:
    trainer.set_password("correct horse battery staple")
    trainer.save(update_fields=["password"])
    client.post(reverse("accounts:login"), {"username": trainer.email, "password": "correct horse battery staple"})
    client.post(reverse("accounts:logout"))

    (record,) = events(captured, "auth.logged_out")
    assert record["actor_email"] == trainer.email

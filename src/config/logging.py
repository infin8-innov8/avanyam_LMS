"""Structured logging for the Prometheus / Loki / Grafana (PLG) stack.

Design
------
Every record this application emits is one line of JSON with the same set of
keys, whether it came from application code (``log.info("undo.code_sent", ...)``)
or from a library we do not control (``django.request``, ``kombu``, Celery). That
consistency is the whole point: a Loki query written once works against every
stream, and a Grafana dashboard can mix an access line with a mail line without
either having been reshaped first.

**Distributed, not one file.** Records are split across named streams --
``access``, ``mail``, ``approval``, ``undo``, ``security``, ``celery``, ``app``
-- and each stream gets its own rotating file. A single file forces two problems
onto whoever reads it: the mail stream drowns in access lines, and anything
urgent has to be grepped out of megabytes of SQL echoes. In Loki each file is a
separate stream with its own label set, so retention and alerting can differ per
concern. An eighth stream, ``error``, is not a domain but a *severity* view:
every record at ``WARNING`` or above is written there too, so "show me the
problems" is one tail rather than seven.

The stream is derived from the event name's first segment, so naming an event
``undo.code_sent`` is all it takes to put it in ``undo.log``. There is no second
place to declare the routing, which means the file a record lands in and the
``stream`` field in its payload cannot drift apart.

**The schema.** These keys are always present:

==============  ==========================================================
``timestamp``   ISO-8601, UTC, millisecond precision, ``Z``-suffixed
``level``       ``debug`` / ``info`` / ``warning`` / ``error`` / ``critical``
``event``       dotted identifier, the primary key for querying
``stream``      which stream the record was routed to
``logger``      originating logger name
``service``     service name, ``avanyam-lms``
``env``         deployment environment
``message``     human-readable sentence, may be empty
==============  ==========================================================

and these are added whenever they are known, never as ``null``:

``request_id``, ``actor_id``, ``actor_email``, ``actor_roles``, ``task_name``,
``trace_id``, ``span_id``, ``duration_ms``, ``outcome``, ``error_type``,
``error``, ``exc_info``, plus whatever fields a call site passes.

Nothing here formats a bound value into the message string. Fields go in as
keyword arguments and stay queryable, which is what lets Grafana compute a
send-failure rate with ``count_over_time`` instead of a regular expression over
prose.

``default=str`` on the JSON encoder is load-bearing rather than lazy: identifiers
in this schema are UUIDs and datetimes, and a single unserialisable value would
otherwise raise inside the logging handler, which swallows the exception and
drops the line -- losing exactly the record logging was installed to capture.

**Reading it back.** ``scripts/watch-logs.sh`` already tails the app's stdout.
The per-stream files are for the machine: Promtail ships them, Loki indexes the
JSON, Grafana queries them. Both paths get the same records, so the terminal view
and the dashboard can never disagree.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from config.observability import snapshot

NOISY_LOGGERS = ("botocore", "boto3", "s3transfer", "urllib3", "asyncio")

#: Every stream that gets its own file. ``error`` is filled in by the router for
#: severity reasons rather than by name-based routing, but it is listed so the
#: full set of files on disk is known up front and can be pre-created.
STREAMS: tuple[str, ...] = (
    "app",
    "access",
    "mail",
    "approval",
    "undo",
    "security",
    "celery",
    "error",
)

#: First segment of the event name decides the stream. This is the only routing
#: rule for application events, and a test walks every event name in the codebase
#: against it -- so an event that would land somewhere surprising fails the suite
#: rather than shipping.
EVENT_STREAM: dict[str, str] = {
    "http": "access",
    "access": "access",
    "mail": "mail",
    "approval": "approval",
    "undo": "undo",
    "auth": "security",
    "otp": "security",
    "security": "security",
    # Privilege changes share the security stream: "who could do what, and when"
    # is one question, and splitting it across files means reading two.
    "roles": "security",
    "celery": "celery",
    "task": "celery",
    "health": "app",
}

#: Fallback for records from libraries, which carry no event name of ours to
#: read. Longest prefix wins, so ``django.request`` is considered before
#: ``django.db.backends`` could ever claim it.
LOGGER_STREAM: tuple[tuple[str, str], ...] = (
    ("django.request", "access"),
    ("django.server", "access"),
    ("django.security", "security"),
    ("celery", "celery"),
    ("kombu", "celery"),
    ("amqp", "celery"),
)

DEFAULT_STREAM = "app"
ERROR_STREAM = "error"
ERROR_THRESHOLD = logging.WARNING

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5

#: Keys the formatter owns. A call site passing one of these is trying to
#: overwrite the envelope, which would make the record unqueryable, so they are
#: dropped rather than honoured.
RESERVED_KEYS = frozenset(
    {"timestamp", "level", "event", "stream", "logger", "service", "env", "message"}
)


def stream_for_event(event: str | None) -> str:
    """Pick the stream for an event name."""
    if not event:
        return DEFAULT_STREAM
    return EVENT_STREAM.get(event.split(".", 1)[0], DEFAULT_STREAM)


def stream_for_logger(name: str) -> str:
    """Pick the stream for a foreign record, by logger name."""
    best = DEFAULT_STREAM
    best_len = -1
    for prefix, stream in LOGGER_STREAM:
        if name.startswith(prefix) and len(prefix) > best_len:
            best, best_len = stream, len(prefix)
    return best


def _iso_utc(epoch: float) -> str:
    """Render an epoch as ``YYYY-MM-DDTHH:MM:SS.mmmZ``.

    Built from the epoch rather than via ``Formatter.formatTime``, for two
    reasons that were both live bugs:

    * ``formatTime`` defaults to ``time.localtime``, so the value it produced was
      the *host's* wall clock. This deployment runs at +05:30, so every timestamp
      was writing 05:30 ahead of UTC while carrying a ``Z`` that claimed
      otherwise. Records from two hosts would have been silently misordered in
      Loki, and a DST change would have shifted them a second time.
    * ``%f`` is not a ``time.strftime`` directive. It was emitted literally, so
      the fraction was the four characters ``.%f`` and the trailing ``[:-3]``
      existed only to trim them -- meaning the "microsecond precision" this
      module documents was never in the output at all.

    Milliseconds rather than microseconds: that is the precision Loki and Grafana
    keep, so going finer would imply an ordering they do not preserve.
    """
    moment = datetime.fromtimestamp(epoch, tz=UTC)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _positional_payload(
    _: Any, __: str, event_dict: dict[str, Any]
) -> tuple[tuple[dict[str, Any]], dict[str, Any]]:
    """Hand the whole event dict to the stdlib logger as ``msg``.

    structlog ends its processor chain in one of four shapes, and the choice
    matters here: a dict returned last is splatted as **kwargs**, which would push
    every field through ``logging``'s ``extra`` handling and lose the ability to
    tell our records from a library's. Returning ``((event_dict,), {})`` puts it
    in the single positional slot instead, so ``record.msg`` *is* the event dict
    and :class:`JsonFormatter` can recognise it.
    """
    return (event_dict,), {}


def _summarise_exception(text: str) -> str:
    """The last line of a rendered traceback, e.g. ``ValueError: bad code``.

    The full traceback goes in ``exc_info`` for when someone is reading an
    incident; ``error`` is the field a dashboard groups and an alert counts, and a
    value carrying newlines and stack frames in it is useless for both.
    """
    for line in reversed(text.strip().splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped
    return text.strip()[:200]


def _safe_message(record: logging.LogRecord) -> str:
    """``record.getMessage()``, guaranteed not to raise.

    Foreign records arrive pre-interpolated by convention -- ``logger.warning("
    Internal Server Error: %s", status)`` -- but nothing enforces it. A single
    library that logs ``("something happened", exc)`` with no ``%s`` makes
    ``msg % args`` raise, and an exception inside a formatter means the record is
    written to ``stderr`` as a logging error and dropped: the one line that
    mattered most, because it was malformed, would be the one that disappeared.
    So the message is best-effort and the raw parts are kept alongside it.
    """
    try:
        return record.getMessage()
    except (TypeError, ValueError):
        raw = record.msg if isinstance(record.msg, str) else repr(record.msg)
        return f"{raw} (unformattable args={record.args!r})"


class JsonFormatter(logging.Formatter):
    """Render any record -- ours or a library's -- as one canonical JSON line."""

    #: Set per-process before logging starts; declared here so they are part of
    #: the formatter's contract rather than attributes bolted on later.
    service = "avanyam-lms"
    env = "unknown"

    @staticmethod
    def _stream_for(event: str, record: logging.LogRecord) -> str:
        """Route by event name, falling back to logger name.

        A record from a library has no event name of ours, so its ``event`` is the
        logger name -- and ``django.request`` must land in ``access``, not in the
        default stream because its first segment is not a namespace we know.
        """
        if event and event != record.name:
            return stream_for_event(event)
        return stream_for_logger(record.name)

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {}

        if isinstance(record.msg, dict):
            # Produced by `_positional_payload` above. Calling getMessage() on it
            # would stringify the whole payload, so the two branches below are
            # not interchangeable.
            payload.update(record.msg)
        else:
            payload["message"] = _safe_message(record)

        # structlog has spelled this key both `exc_info` and `exception` across
        # versions. Both are accepted and the traceback is always emitted as
        # `exc_info`, so a dashboard written against the documented schema does
        # not quietly stop matching after a dependency bump.
        rendered_exc = payload.pop("exc_info", None)
        rendered_exc = payload.pop("exception", None) or rendered_exc

        # Read the envelope's own fields out of the payload *before* stripping
        # reserved keys: `event` and `message` are both reserved and both arrive
        # in the payload, so stripping first silently replaced every event name
        # with the logger name and every message with an empty string.
        event = payload.pop("event", None) or record.name
        message = payload.pop("message", "") or ""
        stream = payload.pop("stream", None) or self._stream_for(event, record)
        # Anything still reserved is a caller trying to overwrite the envelope.
        for key in RESERVED_KEYS:
            payload.pop(key, None)

        envelope: dict[str, Any] = {
            "timestamp": _iso_utc(record.created),
            "level": record.levelname.lower(),
            "event": event,
            "stream": stream,
            "logger": record.name,
            "service": self.service,
            "env": self.env,
            "message": message,
        }

        duration = getattr(record, "duration_ms", None)
        if duration is not None:
            envelope["duration_ms"] = round(float(duration), 3)
        if record.stack_info:
            envelope["stack"] = self.formatStack(record.stack_info)

        # Ambient correlation context. `setdefault` means an explicit field on the
        # record wins, so a Celery task can override a request id it inherited.
        for key, value in snapshot().items():
            envelope.setdefault(key, value)

        # Our records carry an already-rendered traceback (structlog's
        # `format_exc_info`); a library's carries the raw tuple. Both end up in
        # the same field.
        exc_text: str | None = None
        if isinstance(rendered_exc, str) and rendered_exc:
            exc_text = rendered_exc
        elif record.exc_info:
            exc_text = self.formatException(record.exc_info)
        if exc_text:
            envelope["exc_info"] = exc_text
            envelope.setdefault("error", _summarise_exception(exc_text))
            exc_type = record.exc_info[0] if record.exc_info else None
            if exc_type is not None:
                envelope.setdefault("error_type", exc_type.__name__)
            elif "error" in envelope:
                head = envelope["error"].split(":", 1)[0].strip()
                if head:
                    envelope.setdefault("error_type", head)

        envelope.update(payload)
        # A caller may pass `error_type` alongside the exception; keep whichever
        # was supplied and never overwrite it with a guess.
        if exc_text and "error_type" not in envelope:
            head = envelope.get("error", "").split(":", 1)[0].strip()
            if head:
                envelope["error_type"] = head

        try:
            return json.dumps(envelope, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            # Last resort. A field that cannot be encoded must not cost us the
            # record; fall back to a minimal envelope that still says what
            # happened, and that rendering the detail failed.
            return json.dumps(
                {
                    "timestamp": envelope["timestamp"],
                    "level": "error",
                    "event": "log.encode_failed",
                    "stream": DEFAULT_STREAM,
                    "logger": record.name,
                    "service": self.service,
                    "env": self.env,
                    "message": "a log record could not be encoded as JSON",
                    "original_event": str(event),
                    "original_logger": record.name,
                },
                default=str,
            )


class StreamRouterHandler(logging.Handler):
    """Fan one record out to the file handlers for its stream, and to ``error``.

    Routing by event name at the handler, rather than by configuring a separate
    logger per domain, means a record cannot end up on the "wrong" stream because
    of which module emitted it: ``apps.accounts.tasks`` both sends mail and runs
    background work, and its logger name would have to pick a side.
    """

    def __init__(self, handlers: dict[str, logging.Handler]) -> None:
        super().__init__()
        self._by_stream = handlers

    @property
    def streams(self) -> tuple[str, ...]:
        return tuple(self._by_stream)

    def emit(self, record: logging.LogRecord) -> None:
        stream = self.route(record)
        try:
            handler = self._by_stream.get(stream) or self._by_stream.get(DEFAULT_STREAM)
            if handler is not None:
                handler.handle(record)
            if record.levelno >= ERROR_THRESHOLD:
                error_handler = self._by_stream.get(ERROR_STREAM)
                if error_handler is not None and error_handler is not handler:
                    error_handler.handle(record)
        except Exception:  # noqa: BLE001  # pragma: no cover - defensive
            # logging swallows this anyway; doing it explicitly stops the failure
            # being attributed to whatever code happened to emit the record.
            self.handleError(record)

    @staticmethod
    def route(record: logging.LogRecord) -> str:
        """Which stream a record belongs in. Exposed for tests."""
        event: str | None = None
        if isinstance(record.msg, dict):
            raw = record.msg.get("event")
            event = raw if isinstance(raw, str) else None
        if event is None:
            event = getattr(record, "event", None)
        if isinstance(event, str):
            return stream_for_event(event)
        return stream_for_logger(record.name)


def _log_dir() -> Path:
    configured = os.environ.get("DJANGO_LOG_DIR")
    if configured:
        return Path(configured)
    # src/config/logging.py -> parents[2] is the repository root.
    return Path(__file__).resolve().parents[2] / "logs" / "app"


def _to_files() -> bool:
    """Whether to write files at all.

    Files are the default because the streams are the point. A container that
    ships stdout can turn them off with ``DJANGO_LOG_TO_FILES=0`` and route the
    single stdout stream instead -- at the cost of the per-stream split, which is
    a deliberate trade rather than a default.
    """
    return os.environ.get("DJANGO_LOG_TO_FILES", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _max_bytes() -> int:
    return int(os.environ.get("DJANGO_LOG_MAX_BYTES", DEFAULT_MAX_BYTES))


def _backup_count() -> int:
    return int(os.environ.get("DJANGO_LOG_BACKUP_COUNT", DEFAULT_BACKUP_COUNT))


def build_router(*, level: str = "INFO", to_files: bool | None = None) -> StreamRouterHandler:
    """Build the routing handler and everything it writes to.

    A plain function rather than a ``logging.config`` dict factory, because the
    set of handlers is derived from :data:`STREAMS` and the environment: one
    declaration of "which files exist" instead of one per settings module.
    """
    formatter = JsonFormatter()
    formatter.service = os.environ.get("OTEL_SERVICE_NAME", "avanyam-lms")
    formatter.env = os.environ.get("DJANGO_ENV", "unknown")

    write_files = _to_files() if to_files is None else to_files

    handlers: dict[str, logging.Handler] = {}
    if write_files:
        log_dir = _log_dir()
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            # A read-only filesystem must not stop the application booting. Fall
            # back to stdout, which is what a container would have used anyway,
            # and say so loudly enough to notice.
            write_files = False
            sys.stderr.write(
                f"logging: cannot create log dir {log_dir}; falling back to stdout\n"
            )

    if write_files:
        log_dir = _log_dir()
        for stream in STREAMS:
            handler: logging.Handler = logging.handlers.RotatingFileHandler(
                log_dir / f"{stream}.log",
                maxBytes=_max_bytes(),
                backupCount=_backup_count(),
                encoding="utf-8",
                # delay=True: importing settings must not create files as a side
                # effect. A command that logs nothing leaves nothing behind.
                delay=True,
            )
            handler.setLevel(level)
            handler.setFormatter(formatter)
            handlers[stream] = handler
    else:
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(level)
        console.setFormatter(formatter)
        handlers[DEFAULT_STREAM] = console

    return StreamRouterHandler(handlers)


class BoundLogger(structlog.stdlib.BoundLogger):
    """A logger whose second positional argument is the human-readable message.

    ``structlog.stdlib.BoundLogger`` collects extra positional arguments into a
    ``positional_args`` list to support stdlib %-formatting, which is not the
    shape wanted here: the message is a distinct concept from the event name and
    the call sites, and routing it through ``positional_args`` would put it in a
    list in the JSON and make it awkward to filter on.

    With this subclass the natural call is::

        log.info("undo.code_sent", "Undo code emailed", outcome="success")

    where the message is optional and ``message=""`` results when it is omitted.
    """

    def _proxy_to_logger(
        self,
        method_name: str,
        event: str | None = None,
        *event_args: str,
        **event_kw: Any,
    ) -> Any:
        if event_args and isinstance(event_args[0], str) and "message" not in event_kw:
            event_kw["message"], *rest = event_args
            event_args = tuple(rest)
        return super()._proxy_to_logger(
            method_name, *event_args, event=event, **event_kw
        )


#: Whether :func:`configure_structlog` has run. :func:`get_logger` consults this
#: because a module-level ``log = get_logger(__name__)`` can execute before Django
#: has finished building the logging config -- and structlog's ``get_logger``
#: returns a *lazy* proxy which ``.bind()`` resolves immediately, against whatever
#: config exists at that instant. Resolving too early silently yields structlog's
#: default ``BoundLoggerFilteringAtNotset`` instead of :class:`BoundLogger`, and the
#: symptom is not an import error: that class treats the second positional argument
#: as ``%``-interpolation args, so ``log.info("undo.code_sent", "message")`` raises
#: TypeError the first time the level is actually enabled, and drops ``message``
#: from every record. Import order would decide which modules log correctly.
_CONFIGURED = False

def get_logger(name: str, **initial: Any) -> BoundLogger:
    """Return a structured logger with ``initial`` fields bound for its lifetime.

    Args:
        name: the module's ``__name__``, so a record says where it came from.
        initial: fields to bind, e.g. ``get_logger(__name__, component="mail")``.

    Raises:
        ValueError: if ``name`` is not a non-empty string. This is a programming
            error, and failing loudly at import beats a record whose ``logger``
            field cannot be filtered on.
    """
    if not isinstance(name, str) or not name:
        raise ValueError(f"logger name must be a non-empty string, got {name!r}")
    # See `_CONFIGURED`. A bool check, on a path that runs once per module.
    if not _CONFIGURED:
        configure_structlog()
    return structlog.get_logger(name).bind(**initial)




def configure_structlog() -> None:
    """Point structlog at the stdlib logging module.

    structlog provides the *call syntax* -- bound keyword fields, exception
    rendering, and cheap no-op logging when a level is disabled -- while the
    stdlib pipeline does the writing. Routing everything through stdlib is what
    makes a library's own log line come out in the same shape as ours.

    The processor list stops short of rendering: output stays an event dict for
    :class:`JsonFormatter` to turn into the envelope. Two formatters on one
    pipeline is how the halves would drift apart.
    """
    global _CONFIGURED
    logging.captureWarnings(True)
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _positional_payload,
        ],
        wrapper_class=BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        # Every processor above is a name/level/exception rewrite that does not
        # depend on configuration, so caching the bound logger is safe and saves
        # a dict rebuild per call.
        cache_logger_on_first_use=True,
    )
    _CONFIGURED = True


def logging_config(
    level: str,
    sql_debug: bool = False,
    *,
    to_files: bool | None = None,
) -> dict[str, Any]:
    """Build the Django ``LOGGING`` dict.

    Args:
        level: root log level name, e.g. ``INFO``.
        sql_debug: when false, ``django.db.backends`` is pinned to ``WARNING``.
            SQL echoes contain bound parameter values -- i.e. user data, and on
            a misconfigured alias, credentials. Off unless asked for.
        to_files: override the ``DJANGO_LOG_TO_FILES`` environment decision.
    """
    # Called while settings are being imported, before anything can log, so this
    # is the one place structlog needs wiring.
    configure_structlog()

    JsonFormatter.service = os.environ.get("OTEL_SERVICE_NAME", "avanyam-lms")
    JsonFormatter.env = os.environ.get("DJANGO_ENV", "unknown")

    loggers = {name: {"level": "WARNING"} for name in NOISY_LOGGERS}
    if not sql_debug:
        # Bound parameter values in a SQL echo are user data, and on a
        # misconfigured alias, credentials.
        loggers["django.db.backends"] = {"level": "WARNING"}

    # Django installs a DEFAULT_LOGGING before this one, and it leaves two traps
    # behind on the `django` logger: a plain StreamHandler with the *base*
    # formatter, so every record would be emitted twice -- once as JSON here and
    # once as prose on stderr -- and an AdminEmailHandler that mails the ADMINS a
    # copy of anything logged at ERROR. The second is the worse of the two: nobody
    # wants a routine 404 turning into an email, and with the base formatter a
    # record whose args do not match its format string raises inside the handler
    # and is lost from that path entirely.
    #
    # `handlers: []` is what removes them -- an entry in `loggers` replaces that
    # logger's handler list outright -- and `propagate: True` lets records reach
    # the single router on the root logger. Attaching the router to `django`
    # directly instead would also work, but it builds a second router instance
    # with its own eight file handles, so the same log line gets written twice
    # and rotation happens per-handler rather than per-file.
    #
    # `django.server` arrives with propagate=False, which would keep runserver's
    # request lines out of the access stream entirely, so that is reset too.
    loggers["django"] = {"handlers": [], "level": level, "propagate": True}
    loggers["django.server"] = {"level": level, "propagate": True}

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "handlers": {
            # The router is a factory function because the handler set behind it
            # is derived from STREAMS and the environment, not spelled out here.
            "router": {"()": "config.logging.build_router", "level": level},
        },
        "loggers": {
            **loggers,
            "": {"handlers": ["router"], "level": level},
        },
    }
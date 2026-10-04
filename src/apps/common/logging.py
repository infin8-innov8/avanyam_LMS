"""The logger application code should use.

One import, one call shape, and a set of rules that a test can enforce, rather
than every module reaching for :func:`logging.getLogger` and inventing its own
event names. The rules exist because the alternative is what this repository had
before: seven hand-formatted strings in the whole codebase, no way to query them,
and no way to tell from a log line whether a mail was sent or only attempted.

Usage::

    from apps.common.logging import get_logger

    log = get_logger(__name__)

    log.info(
        "undo.code_sent",
        "Undo code emailed to the trainer",
        outcome="success",
        request_pk=req.pk,
        duration_ms=5312,
    )

The first argument is the event name and is the primary key for querying: it must
be dotted lowercase, must start with a namespace in
:data:`config.logging.EVENT_STREAM` so the record lands in the right stream, and
must be stable -- renaming one is a breaking change for every dashboard and alert
query built on it. The second argument is optional prose for humans reading the
raw JSON; it is never what you query.

Levels carry meaning here and are not interchangeable:

``debug``
    Detail useful while developing. Off by default.
``info``
    Something happened that a reader of the system would want to see: a mail went
    out, an application was reversed, a request completed.
``warning``
    The system coped but something is wrong: a mail had no recipients, a token's
    delivery stamp could not be written, a row was in an unexpected state.
``error``
    An operation the caller asked for failed. For a request, this is a 5xx or a
    refusal the user could not have caused.
``critical``
    The process cannot keep serving. Not used in application code.
"""

from __future__ import annotations

import re

from config.logging import BoundLogger, get_logger

__all__ = ["BoundLogger", "check_event", "get_logger"]

#: Event names are dotted lowercase identifiers. The pattern rejects spaces,
#: sentences and CamelCase, all of which turn up in a log stream as a field that
#: cannot be selected on without quoting gymnastics in LogQL.
EVENT_NAME = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")

#: Namespaces with no stream of their own, kept short so call sites do not
#: collide with something meaningful later. `task` is an alias of `celery` for
#: background work; `access` of `http` for request-shaped events.
ALLOWED_NAMESPACES = frozenset(
    {
        "access",
        "app",
        "approval",
        "auth",
        "celery",
        "health",
        "http",
        "mail",
        "otp",
        # Privilege changes. Routed to the same stream as sign-in because a role
        # grant is a grant of authority, and "who could do what, and when" is the
        # question somebody reads that stream to answer.
        "roles",
        "security",
        "task",
        "undo",
    }
)


def check_event(event: str) -> str:
    """Validate an event name. Raises :class:`ValueError` when unusable.

    Kept as a function rather than a decorator so it can be applied to the event
    names a test finds in the source, including inside ``log.warning(...)`` calls
    that no unit test happens to execute.
    """
    if not isinstance(event, str) or not EVENT_NAME.match(event):
        raise ValueError(
            f"event name {event!r} is not a dotted lowercase identifier "
            "(expected something like 'undo.code_sent')"
        )
    namespace = event.split(".", 1)[0]
    if namespace not in ALLOWED_NAMESPACES:
        raise ValueError(
            f"event name {event!r} starts with {namespace!r}, which is not a known "
            f"namespace; expected one of {sorted(ALLOWED_NAMESPACES)}"
        )
    return event
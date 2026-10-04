"""Correlation context for log records.

Kept apart from :mod:`config.logging` so that anything which wants to *bind*
context -- middleware, management commands, Celery task wrappers, the test
client -- does not have to import the settings-driven configuration module and
risk configuring logging twice.

Everything here is stored in :mod:`contextvars`, not thread locals, because the
request is not always handled by the thread that started it: WSGI servers hand
work to a worker pool, and ASGI runs several requests on one thread. A
thread-local would leak one trainer's request id into another trainer's log line,
which is worse than having no id at all.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

#: Correlates every line produced while handling one HTTP request, one Celery
#: task, or one management command.
_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)

#: Who is doing the thing: ``{"id": .., "email": .., "roles": [..]}``. Bound
#: after authentication resolves, so lines emitted by the login view itself are
#: correctly anonymous.
_actor: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "actor", default=None
)

#: Set by the Celery task wrapper so a dispatch and its execution share an id.
_task_name: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "task_name", default=None
)


def get_request_id() -> str | None:
    return _request_id.get()


def set_request_id(value: str) -> None:
    _request_id.set(value)


def get_actor() -> dict[str, Any] | None:
    return _actor.get()


def set_actor(actor: dict[str, Any] | None) -> None:
    _actor.set(actor)


def get_task_name() -> str | None:
    return _task_name.get()


def snapshot() -> dict[str, Any]:
    """Return the ambient context as log fields.

    Keys are omitted rather than emitted as ``null`` so that a line from a
    management command does not carry three empty correlation fields.
    """
    fields: dict[str, Any] = {}
    request_id = _request_id.get()
    if request_id:
        fields["request_id"] = request_id
    actor = _actor.get()
    if actor:
        fields.update(actor)
    task_name = _task_name.get()
    if task_name:
        fields["task_name"] = task_name
    return fields


@contextmanager
def request_scope(request_id: str) -> Iterator[None]:
    """Bind a request id for the duration of the block, then restore.

    Restoring rather than clearing matters in tests and in the reuse of a single
    worker thread: a stale id left behind would be attached to whatever ran next.
    """
    token = _request_id.set(request_id)
    try:
        yield
    finally:
        _request_id.reset(token)


@contextmanager
def actor_scope(actor: dict[str, Any] | None) -> Iterator[None]:
    token = _actor.set(actor)
    try:
        yield
    finally:
        _actor.reset(token)


@contextmanager
def task_scope(task_name: str) -> Iterator[None]:
    token = _task_name.set(task_name)
    try:
        yield
    finally:
        _task_name.reset(token)


def describe_user(user: Any) -> dict[str, Any]:
    """Reduce a user object to the fields worth logging.

    The email is included deliberately: when a support question arrives, the
    fastest way to find the relevant lines is to search the address the person
    reports. Passwords, tokens and codes never come near this.
    """
    if user is None:
        return {}
    if not getattr(user, "is_authenticated", False):
        return {}
    roles = sorted(getattr(user, "roles", None) or [])
    fields: dict[str, Any] = {
        "actor_id": getattr(user, "pk", None),
        "actor_email": getattr(user, "email", None),
    }
    if roles:
        fields["actor_roles"] = roles
    return fields
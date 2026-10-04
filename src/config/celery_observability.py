"""Task lifecycle events, wired through Celery's own signals.

Without this, `task_name` is a field in the documented schema that nothing ever
populates, `celery.log` holds only the incidental records the tasks emit by hand,
and a dispatched task has nothing tying it to the execution that follows. All
three are gaps rather than absences: the schema claims the field, the stream
exists, and nothing fills either.

Signals rather than a decorator on each task, on purpose. Six call sites
decorated by hand means the seventh -- the one added next quarter -- is unlogged,
and nothing fails when that happens. A signal cannot be forgotten.

**How context is bound.** `task_prerun` fires in the worker process immediately
before the task body and `task_postrun` immediately after, in the same thread, so
a :class:`~config.observability.ContextVar` set in one is readable in the other
and throughout the task body. That holds for the prefork pool this project uses.
It would *not* hold for a thread or eventlet pool, where the callbacks can land
on a different thread from the task body; `_pool_is_prefork` asserts the pool so
changing it fails loudly here instead of silently dropping the correlation fields
from every record.

`task_postrun` does not fire if the worker is killed mid-task, so the previous
task's context could survive into the next one. `task_prerun` rebinds both fields
unconditionally, which makes that self-healing rather than cumulative.

Timing uses a monotonic clock, so a wall-clock step mid-task cannot produce a
negative duration.
"""

from __future__ import annotations

import time
from typing import Any

from celery import signals
from django.conf import settings

from apps.common.logging import get_logger
from config.observability import request_scope, task_scope

logger = get_logger(__name__)

#: ``task_id -> monotonic start``. Celery hands the same id to prerun and postrun,
#: which is what pairs the two without threading state through the task body.
#: Entries are popped in postrun, so this cannot grow without bound.
_STARTED: dict[str, float] = {}

#: Fallback correlation id when a task somehow arrives without one.
_NO_ID = "no-task-id"


def _pool_is_prefork() -> None:
    if settings.CELERY_TASK_ALWAYS_EAGER:
        return
    pool = getattr(settings, "CELERY_WORKER_POOL", "prefork")
    if pool not in {"prefork", None}:
        raise RuntimeError(
            "config.celery_observability binds task context with ContextVars, which "
            f"only works on the prefork pool. CELERY_WORKER_POOL is {pool!r}. Either "
            "switch back to prefork or move the binding into a task decorator."
        )


def _correlation_id(task_id: str) -> str:
    """Reuse the single correlation slot rather than adding a second one.

    `config.observability` documents `request_id` as covering "one HTTP request,
    one Celery task, or one management command" -- a task is not a second kind of
    id, it is a second user of the same one. So a task's Celery id goes into
    `request_id`, which keeps every line from a task correlated with every other
    line from that task without a schema change. The field name reads oddly on a
    mail line, which is the price of not inventing `task_id` alongside it.
    """
    return task_id or _NO_ID


@signals.task_prerun.connect
def _bind(sender: Any = None, task_id: str = "", task: Any = None, **_kwargs: Any) -> None:
    _pool_is_prefork()
    _STARTED[task_id] = time.perf_counter()
    name = getattr(task, "name", None) or str(task)
    with request_scope(_correlation_id(task_id)), task_scope(name):
        logger.debug(
            "celery.task_started",
            "Task is about to run",
            outcome="started",
            task_id=task_id or None,
        )


@signals.task_postrun.connect
def _finish(
    sender: Any = None,
    task_id: str = "",
    task: Any = None,
    state: str = "",
    **_kwargs: Any,
) -> None:
    started = _STARTED.pop(task_id, None)
    name = getattr(task, "name", None) or str(task)
    duration = round((time.perf_counter() - started) * 1000, 3) if started else None
    with request_scope(_correlation_id(task_id)), task_scope(name):
        logger.info(
            "celery.task_finished",
            "Task returned",
            outcome="success" if state == "SUCCESS" else "failure",
            task_id=task_id or None,
            state=state,
            duration_ms=duration,
        )


@signals.task_failure.connect
def _failed(
    sender: Any = None,
    task_id: str = "",
    exception: BaseException | None = None,
    **_kwargs: Any,
) -> None:
    """Structured failure record, logged before postrun discards the exception.

    Celery's own handler already writes the full traceback for the worker log, so
    this deliberately carries the type and message rather than a second copy of
    the traceback -- the structured stream is for querying and alerting, and a
    duplicate stack trace there costs parsing without adding reachability.

    `task_failure` fires before `task_postrun`, so this is the last point at which
    the exception is available as an object at all.
    """
    name = getattr(sender, "name", None) or str(sender)
    with request_scope(_correlation_id(task_id)), task_scope(name):
        logger.error(
            "celery.task_failed",
            "Task raised",
            outcome="failure",
            task_id=task_id or None,
            error_type=type(exception).__name__ if exception else None,
            error=str(exception)[:200] if exception else None,
        )
"""Request correlation and access logging.

Placed first in ``MIDDLEWARE`` so that everything it wraps -- including
``SecurityMiddleware`` and the password-change gate -- runs inside a request
scope and gets the same ``request_id`` on its log lines.

The middleware exists to solve one problem: without a shared identifier, the log
line that says a mail failed and the log line that says which request asked for
the code are two unrelated rows in two files. With it, one ``request_id`` answers
"what happened during this request" across every stream at once.

An inbound ``X-Request-ID`` is honoured when it looks safe, so a request that
arrives from nginx, an API gateway or a front-end proxy keeps the id the rest of
the infrastructure already knows it by. Anything not matching
:data:`SAFE_REQUEST_ID` is replaced rather than trusted: the header is attacker
controlled, and an unvalidated value lands in every log line for the request and
in the response header, which makes both a log-injection vector and a way to blow
up a dashboard's cardinality with megabyte-long label values.
"""

from __future__ import annotations

import re
import time
import uuid

from config.logging import get_logger
from config.observability import actor_scope, describe_user, request_scope

#: Deliberately strict. Real ids from proxies and tracing systems are hex or
#: UUID-ish; anything else is replaced.
SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._\-]{8,64}$")

HEADER = "X-Request-ID"

#: Probes are called every few seconds by an orchestrator. Logging them at INFO
#: buries real traffic, and they are also the requests most likely to fail during
#: a deploy, which is when their errors matter most -- those are kept.
QUIET_PATHS = ("/livez", "/readyz", "/static/", "/media/", "/favicon.ico")

log = get_logger(__name__)


def _wants_quiet(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in QUIET_PATHS)


class RequestContextMiddleware:
    """Bind a request id, time the request, and log one line per request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.META.get("HTTP_X_REQUEST_ID", "")
        request_id = incoming if SAFE_REQUEST_ID.match(incoming) else uuid.uuid4().hex

        # The scope is the only thing that sets the contextvar, and it restores it
        # on the way out. Setting it out here as well -- which looked harmless --
        # left the id bound to the worker thread after the response was returned,
        # so every later log line on that thread carried a stale request id.
        started = time.perf_counter()
        with request_scope(request_id):
            try:
                response = self.get_response(request)
            except Exception:
                # Logged here because this is the only place that still knows the
                # request was in flight. Django's own handler turns the exception
                # into a 500 further up, so without this line a failure would
                # appear in the logs with no request attached to it at all.
                log.exception(
                    "http.request.exception",
                    "Unhandled exception while handling the request",
                    outcome="failure",
                    method=request.method,
                    path=request.path,
                    duration_ms=round((time.perf_counter() - started) * 1000, 3),
                )
                raise

            status = response.status_code
            duration_ms = round((time.perf_counter() - started) * 1000, 3)

            # The actor is bound only here, after the view has run: `request.user`
            # is a lazy object, and reading it earlier would force the session
            # lookup on requests that never needed it.
            with actor_scope(describe_user(getattr(request, "user", None))):
                self._log_completion(request, status, duration_ms)

            response[HEADER] = request_id
            return response

    def _log_completion(self, request, status: int, duration_ms: float) -> None:
        path = request.path
        fields = {
            "outcome": "success" if status < 400 else "failure",
            "method": request.method,
            "path": path,
            "status": status,
            "duration_ms": duration_ms,
        }

        if status >= 500:
            # `error`, so it lands in the shared error stream alongside everything
            # else that needs attention, rather than only in access.log where a
            # reader would not think to look.
            log.error(
                "http.request.failed",
                "Request failed with a server error",
                **fields,
            )
            return

        if status >= 400:
            log.warning("http.request.rejected", "Request refused", **fields)
            return

        if _wants_quiet(path):
            log.debug("http.request.completed", "Request completed", **fields)
            return

        # A slow request is worth surfacing on its own, not only inside the
        # access stream: 1s is slow for this application but not for a file
        # upload, and the useful threshold differs per route.
        if duration_ms >= 1000:
            log.warning(
                "http.request.slow",
                "Request took longer than the expected budget",
                **fields,
            )
            return

        log.info("http.request.completed", "Request completed", **fields)
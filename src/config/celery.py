"""Celery application.

Discovered via the `CELERY_APP` entry below, which is how the worker and beat
find it without the task modules importing this file first.

Scheduler is **DB-backed** (`django-celery-beat`), not static schedule files --
`architecture.md` rules that out explicitly, because static schedules require a
redeploy to change.
"""
import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

from celery import Celery  # noqa: E402

import config  # noqa: F401,E402  (package import establishes the app namespace)

app = Celery("avanyam")

# All Celery config lives in Django settings with a `CELERY_` prefix, so there is
# exactly one source of truth for broker/result URLs.
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self):  # pragma: no cover - operational aid
    """Print the request. Confirms a worker is alive and wired to a broker."""
    print(f"Request: {self.request!r}")

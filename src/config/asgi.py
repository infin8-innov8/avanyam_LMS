"""ASGI entrypoint.

Kept alongside WSGI so the app can move to async without a restructure, but note
`uvicorn` is NOT installed -- gunicorn is the ASGI server here
(`gunicorn -k uvicorn.workers.UvicornWorker` would fail until it is).
"""
import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

from django.core.asgi import get_asgi_application  # noqa: E402

application = get_asgi_application()

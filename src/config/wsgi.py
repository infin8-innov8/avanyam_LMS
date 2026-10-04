"""WSGI entrypoint (gunicorn).

The `src/` path insert mirrors `manage.py`. It is needed because gunicorn is
invoked as `gunicorn config.wsgi:application` from the repository root, where
`src/` is not importable by default.
"""
import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

from django.core.wsgi import get_wsgi_application  # noqa: E402

application = get_wsgi_application()

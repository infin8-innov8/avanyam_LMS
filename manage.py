#!/usr/bin/env python
"""Django's command-line utility.

Layout follows `architecture.md` §16.1: `manage.py` at the repository root, the
importable code under `src/`, settings at `config.settings.<env>`.

Why not `avanyam_terra.settings`: §16.1 records that **VM3 (aqua) runs the same
application image as VM1**, differing only in the command it runs. A settings
module named after a host would therefore name the wrong host the moment the same
code runs on aqua. `config.settings.*` is host-agnostic by construction.

The `src/` entry on `sys.path` is inserted explicitly rather than via an editable
install: there are three venvs on two hosts that are install *targets* of one
`pyproject.toml`, and a `.pth` file per venv would be invisible and easy to lose.
"""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and available "
            "on your PYTHONPATH? Did you forget to activate the virtual "
            "environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()

"""Logging configuration.

Kept as its own module (architecture.md §16.1 lists ``config/logging.py``)
rather than inline in ``settings/base.py`` so the policy is testable without
importing settings.

The noisy-logger floor is not cosmetic. ``botocore`` at DEBUG logs the full
SigV4 ``CanonicalRequest`` / ``StringToSign`` / ``Authorization`` header for
every S3 call, so a root logger at DEBUG writes request signature material into
the log file on every media operation. It also makes ``readyz`` unreadable.
Anything that wants botocore detail must ask for it explicitly, by logger name.
"""

from __future__ import annotations

from typing import Any

NOISY_LOGGERS = ("botocore", "boto3", "s3transfer", "urllib3", "asyncio")


def logging_config(level: str, sql_debug: bool = False) -> dict[str, Any]:
    """Build the Django ``LOGGING`` dict.

    Args:
        level: root log level name, e.g. ``INFO``.
        sql_debug: when false, ``django.db.backends`` is pinned to ``WARNING``.
            SQL echoes contain bound parameter values -- i.e. user data, and on
            a misconfigured alias, credentials. Off unless asked for.
    """
    loggers = {name: {"level": "WARNING"} for name in NOISY_LOGGERS}
    if not sql_debug:
        loggers["django.db.backends"] = {"level": "WARNING"}
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {
                "format": "%(asctime)s %(levelname)-8s %(name)s %(message)s",
            },
        },
        "handlers": {
            "console": {"class": "logging.StreamHandler", "formatter": "standard"},
        },
        "root": {"handlers": ["console"], "level": level},
        "loggers": loggers,
    }

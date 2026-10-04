"""Test settings.

A separate module rather than a flag on `dev`, so that nothing a test needs can
leak into a running development server and nothing a dev server needs (real
SMTP, axes lockout, slow password hashing) slows the suite down.

Three things this module exists to guarantee:

1. **No real mail.** `locmem` collects messages in `django.core.mail.outbox`.
   A test suite that can send mail through a live Gmail relay is a test suite
   that eventually emails a customer.
2. **No real Redis / broker.** Celery runs tasks eagerly and inline, so
   `notify_trainer_of_application.delay(...)` executes in-process. That keeps the
   tests deterministic and removes the worker from the test's critical path.
   It also means `transaction.on_commit` hooks really do run, so a test can
   assert on the mail a signup produced.
3. **No lockouts.** django-axes is disabled. Axes counts failures per IP across
   the whole test session, so a handful of deliberately bad login attempts in one
   test file would lock out every subsequent test -- a real source of flaky
   suites.
"""

import os

from .base import *  # noqa: F401,F403
from .base import env

DEBUG = False

# pytest's test client uses this host.
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]

# ---------------------------------------------------------------------------
# WHICH DATABASE THE TESTS RUN AGAINST -- read this before adding a test
#
# Normally pytest-django creates a throwaway `test_avanyam` database. That needs
# CREATEDB, and on this host no role has it: avanyam_migrate is not a superuser
# and there is no peer-auth path to the postgres superuser. So instead:
#
#   * `default` is re-pointed at the MIGRATE role, because the suite applies
#     migrations and therefore needs DDL. (Consequence: these tests do not
#     exercise avanyam_app's privileges. test_db_privileges.py covers that
#     separately, by connecting as avanyam_app.)
#   * `TEST["NAME"]` names the EXISTING database, and `MIGRATE` is left on, so
#     Django reuses the database instead of trying to create one.
#   * Every test below uses the plain `db` fixture, which wraps each test in a
#     transaction and ROLLS IT BACK. The seeded development data is untouched.
#
# THE CAVEAT, STATED PLAINLY: this suite runs against your live development
# database. A test that commits for real, or that uses TransactionTestCase
# (which TRUNCATEs tables afterwards), WILL DESTROY YOUR SEEDED ACCOUNTS.
# Do not add either without switching to a real test database first.
# ---------------------------------------------------------------------------
DATABASES["default"] = {  # noqa: F405
    **DATABASES["migrate"],  # noqa: F405
    "TEST": {"NAME": DATABASES["migrate"]["NAME"]},  # noqa: F405
}

# Refuse anything that could truncate the live database.
DATABASES["default"]["TEST"]["MIGRATE"] = True

# `default` is now the migration role, so the suite cannot see what the *web*
# role is allowed to do -- which is the boundary that actually matters in
# production. This alias keeps the genuine runtime credentials reachable, so
# test_db_privileges.py can connect as avanyam_app and assert its permissions.
_PG = DATABASES["default"]  # noqa: F405
DATABASES["runtime"] = {
    "ENGINE": _PG["ENGINE"],
    "HOST": _PG["HOST"],
    "PORT": _PG["PORT"],
    "NAME": env("DB_NAME"),
    "USER": env("DB_USER"),
    "PASSWORD": env("DB_PASSWORD"),
}

# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------
# PBKDF2 with a high iteration count is correct in production and wrong here: it
# would add minutes to a suite that creates dozens of users. MD5 is never
# appropriate outside a test, which is exactly why it is set here and only here.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
DEFAULT_FROM_EMAIL = "test@avanyam.invalid"

# ---------------------------------------------------------------------------
# Celery -- eager, in-process
# ---------------------------------------------------------------------------
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_BROKER_URL = "memory://"
CELERY_RESULT_BACKEND = "cache+memory://"

# Setting the two variables above is NOT sufficient, and the reason is worth
# knowing before anyone "simplifies" this block. `base.py` reads the real
# broker out of `.env` via django-environ, and django-environ also copies it into
# os.environ. Celery layers its configuration as: Django settings FIRST, then
# os.environ on top. So the environment silently wins, the app resolved the live
# Redis URL, and `CELERY_BROKER_URL = "memory://"` above was decoration.
#
# That left exactly one thing stopping the suite from publishing tasks onto the
# live broker: eager mode, which never opens a connection. Any future test that
# disabled eager, or any eager task that reached for the app's broker/result
# backend, would have talked to the real Redis. Clearing the variables keeps the
# promise in this module's docstring true by construction rather than by luck.
for _leaked in ("CELERY_BROKER_URL", "CELERY_RESULT_BACKEND"):
    os.environ.pop(_leaked, None)

# ---------------------------------------------------------------------------
# Axes off (see module docstring)
# ---------------------------------------------------------------------------
AXES_ENABLED = False

# ---------------------------------------------------------------------------
# WhiteNoise
# ---------------------------------------------------------------------------
# collectstatic has never been run against this checkout, so STATIC_ROOT does not
# exist. WhiteNoise warns about that on every single request, which buried real
# warnings under ~40 lines of noise. In tests the static files are served through
# the finders anyway, so the missing directory is expected, not a problem.
WHITENOISE_AUTOREFRESH = True
WHITENOISE_USE_FINDERS = True

# The portal emails contain absolute links; a fixed host keeps assertions stable.
SITE_URL = "http://testserver"

# Signup is exercised directly against the service in these tests, so the
# environment kill switch must not depend on a developer's `.env`.
ACCOUNTS = {**ACCOUNTS, "SIGNUP_ENABLED": True}  # noqa: F405

LOGGING = logging_config("WARNING", sql_debug=False)  # noqa: F405

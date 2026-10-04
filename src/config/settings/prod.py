"""Production.

Everything that is a development convenience is off, and anything that would be
a security finding in a review is asserted rather than assumed:

* `DEBUG` is forced **off** regardless of what the environment says. A stray
  `DJANGO_DEBUG=True` in a deployed `.env` must not become a disclosure bug.
* `ALLOWED_HOSTS` must be non-empty -- an empty list with `DEBUG=False` makes
  Django reject every request, which is a safe failure, but a silent one. This
  turns it into a loud boot failure instead.
* HSTS, secure cookies and SSL redirect are unconditional.

`check --deploy` is expected to pass against this module before release.
"""
from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F401,F403
from .base import env

DEBUG = False

ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=[])
if not ALLOWED_HOSTS:
    raise ImproperlyConfigured(
        "DJANGO_ALLOWED_HOSTS must be set in production. Refusing to boot with "
        "an empty allowlist."
    )

INTERNAL_IPS = ["127.0.0.1"]

SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 365  # 1 year
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
# Do not leak the framework version on error pages.
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# Connection settings (host/port/user/password/TLS) are shared and live in
# `base`; only the backend and the fail-loud check belong here. An unset relay
# in production is a configuration error, not a reason to silently discard
# approval and password-reset mail.
EMAIL_BACKEND = env("DJANGO_EMAIL_BACKEND", default="django.core.mail.backends.smtp.EmailBackend")
if not EMAIL_HOST or not EMAIL_HOST_USER:
    raise ImproperlyConfigured(
        "DJANGO_EMAIL_HOST and DJANGO_EMAIL_HOST_USER must be set in production. "
        "Refusing to boot with mail silently undeliverable."
    )

# Signed cookies are not enough for sessions here; keep server-side sessions and
# rotate the cookie on login.
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_AGE = 60 * 60 * 12
SESSION_SAVE_EVERY_REQUEST = False

# Long-lived connections are safe behind PgBouncer only on a transaction or
# session pool. `default` (avanyam) is `transaction`, so CONN_MAX_AGE > 0 is
# correct; the `migrate` alias in base.py sets it to 0 regardless.
CONN_MAX_AGE = 600

LOGGING = logging_config(env("DJANGO_LOG_LEVEL", default="WARNING"))  # noqa: F405

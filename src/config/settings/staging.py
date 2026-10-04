"""Staging -- production-shaped, separate data.

Per `architecture.md`: real PostgreSQL, Redis, SeaweedFS and ClamAV, a **separate
Keycloak realm** and OpenLDAP instance with seeded data, TLS, anonymized dataset.

That last clause is why staging exists as its own module rather than reusing
`prod` with different env vars: the *shape* must match production or the staging
result means nothing, but the data must never be the real data. Two modules make
that difference reviewable instead of a runtime flag.
"""
from .base import *  # noqa: F401,F403
from .base import env

DEBUG = env.bool("DJANGO_DEBUG", default=False)

ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["staging.internal"])
INTERNAL_IPS = ["127.0.0.1"]

SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 7  # 1 week
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
X_FRAME_OPTIONS = "DENY"

EMAIL_BACKEND = env("DJANGO_EMAIL_BACKEND", default="django.core.mail.backends.smtp.EmailBackend")

LOGGING = logging_config(env("DJANGO_LOG_LEVEL", default="INFO"))  # noqa: F405

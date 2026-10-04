"""Development settings -- this laptop.

Selected by `DJANGO_SETTINGS_MODULE=config.settings.dev`, which is what
`avanyam_terra/.env` and `.env.example` now both say.

Kept honest about what it is: `DEBUG` still comes from `.env`
(`DJANGO_DEBUG=True` on this host) rather than being forced on here, so turning
it off locally is possible and visible. Everything that would be unsafe outside
a developer machine -- permissive hosts, the browsable API, the debug toolbar
style error pages -- lives in this file and nowhere else, so `prod` cannot
inherit it by accident.
"""
from .base import *
from .base import env

DEBUG = env.bool("DJANGO_DEBUG", default=True)

# Loopback only. Not `*` -- an empty list plus DEBUG lets Django fall back to
# localhost, which is what we want, but being explicit documents the boundary.
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])
INTERNAL_IPS = ["127.0.0.1"]

# Browsable API is a development affordance.
REST_FRAMEWORK = {
    **REST_FRAMEWORK,
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
}

# Security middleware stays off in dev only because there is no TLS terminator in
# front of the dev server. `prod` turns all of these on; see `prod.py`.
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SECURE_HSTS_SECONDS = 0

# Mail to the console instead of a relay that does not exist here.
# Defaults to the console so an unconfigured checkout does not send real mail,
# but `.env` can select the SMTP backend and send for real.
EMAIL_BACKEND = env("DJANGO_EMAIL_BACKEND", default="django.core.mail.backends.console.EmailBackend")

# HSTS off, but the rest of the hardening visible so it is obvious what prod adds.
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# Axes stays on even in dev: lockout behaviour is worth exercising locally before
# it is relied on in production.
AXES_ENABLED = True

LOGGING = logging_config(
    env("DJANGO_LOG_LEVEL", default="INFO"), sql_debug=env.bool("DJANGO_SQL_DEBUG", default=False)
)

"""Settings shared by every environment.

Design rules, each of which exists because the obvious alternative was wrong:

1. **This module reads `.env` itself.** `manage.py` does not, and neither does
   `gunicorn` or `celery`. The stock `startproject` settings that were here
   ignored `.env` entirely -- hardcoded an insecure `SECRET_KEY`, `DEBUG = True`
   and SQLite -- so a fully-verified four-role PostgreSQL cluster sat unused
   while the app quietly used a local file. Loading here means every entrypoint
   gets the same values with no wrapper script.

2. **No secret has an inline default.** A missing `DJANGO_SECRET_KEY` must crash
   at boot, not fall back to a value that is in the repository.

3. **Real environment variables win over `.env`.** `environ.Env.read_env` does
   not override, so container/systemd injection still takes precedence.

4. **Database roles are separated, not shared.** `architecture.md` §14.3 gives
   four roles distinct jobs; `default` runs as the least-privileged runtime role
   and migrations run as `avanyam_migrate`. See `DATABASES` below.
"""
from pathlib import Path

import environ

from config.logging import logging_config

# src/config/settings/base.py -> parents[3] is the repository root.
REPO_ROOT = Path(__file__).resolve().parents[3]
BASE_DIR = REPO_ROOT

# avanyam_terra/.env is the single credential file for VM1. aqua has its own.
ENV_FILE = REPO_ROOT / "avanyam_terra" / ".env"
if ENV_FILE.exists():
    environ.Env.read_env(str(ENV_FILE))

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
    DJANGO_ALLOWED_HOSTS=(list, []),
    AWS_QUERYSTRING_AUTH=(bool, True),
    AWS_QUERYSTRING_EXPIRE=(int, 900),
    AWS_DEFAULT_ACL=(str, "private"),
)

# --------------------------------------------------------------------------
# Core
# --------------------------------------------------------------------------
# No default. If this raises, the fix is to supply the key -- not to add one here.
SECRET_KEY = env("DJANGO_SECRET_KEY")

# The environment modules must set these; base deliberately does not.
DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=[])

# `localhost,127.0.0.1` style values come in as one comma-separated string.
if "" in ALLOWED_HOSTS:
    ALLOWED_HOSTS = [h for h in ALLOWED_HOSTS if h]

INSTALLED_APPS = [
    # Django
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Bounded contexts live under `apps.*` (§3). `common` holds only the shared
    # abstract models; `accounts` is the first context implemented.
    "apps.common",
    "apps.accounts",
    "apps.pages",
    # Third-party (all pinned in uv.lock, all verified importable on this host)
    "rest_framework",
    "django_filters",
    "drf_spectacular",
    "simple_history",
    "axes",
    "csp",
]

MIDDLEWARE = [
    # Outermost: binds a request id and times the request, so every line logged
    # by the middleware below it carries the same correlation id.
    "config.middleware.RequestContextMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "simple_history.middleware.HistoryRequestMiddleware",
    # Brute-force lockout. Must sit after AuthenticationMiddleware.
    "axes.middleware.AxesMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# Templates live in `src/frontend/templates` and assets in `src/frontend/static`
# (§16.1). WhiteNoise serves the collected output in production, so DEBUG=False
# does not mean unstyled pages.
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "src" / "frontend" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

STATICFILES_DIRS = [BASE_DIR / "src" / "frontend" / "static"]

# STATIC_ROOT is defined once, in the "i18n / static" section below. It used to
# be set here too, with a different default (/var/lib/avanyam/static). Because
# Python takes the last assignment, this earlier value was dead code that
# silently did nothing -- the effective default came from the later line. Only
# one definition now; production sets DJANGO_STATIC_ROOT explicitly.

# `frontend/static/css/tokens.css` is the single source of design tokens (§16.1).
# It sits under STATICFILES_DIRS, so templates reach it as `css/tokens.css` and
# WhiteNoise picks it up from STATIC_ROOT in production.
STATICFILES_FINDERS = [
    "django.contrib.staticfiles.finders.FileSystemFinder",
    "django.contrib.staticfiles.finders.AppDirectoriesFinder",
]

# --------------------------------------------------------------------------
# Accounts (§15)
# --------------------------------------------------------------------------
ACCOUNTS = {
    # Kill switch for registration (§15). Flip with an env change, no redeploy.
    "SIGNUP_ENABLED": env.bool("ACCOUNTS_SIGNUP_ENABLED", default=True),
}

# Absolute base for links in emails. Celery and the web process must agree, or
# a "Review this request" link points at nothing.
SITE_URL = env("SITE_URL", default="http://127.0.0.1:8000")

# --------------------------------------------------------------------------
# Databases -- four roles, four purposes (architecture.md §14.3)
# --------------------------------------------------------------------------
_PG = {
    "ENGINE": "django.db.backends.postgresql",
    "HOST": env("DB_HOST", default="localhost"),
    "PORT": env("DB_PORT", default="5432"),
    "CONN_MAX_AGE": env.int("DB_CONN_MAX_AGE", default=60),
    "CONN_HEALTH_CHECKS": True,
    "OPTIONS": {"sslmode": env("DB_SSLMODE", default="prefer")},
}

DATABASES = {
    # Runtime role. Owns the schema; no DDL rights beyond what it was granted.
    "default": {
        **_PG,
        "NAME": env("DB_NAME"),
        "USER": env("DB_USER"),
        "PASSWORD": env("DB_PASSWORD"),
    },
    # Migration role, used by `migrate`. Separate so a compromised web process
    # cannot alter the schema.
    "migrate": {
        **_PG,
        "NAME": env("DB_MIGRATE_NAME"),
        "USER": env("DB_MIGRATE_USER"),
        "PASSWORD": env("DB_MIGRATE_PASSWORD"),
        # DDL churn should not hold a long-lived connection open.
        "CONN_MAX_AGE": 0,
    },
    # Append-only audit trail.
    "audit": {
        **_PG,
        "NAME": env("DB_AUDIT_NAME"),
        "USER": env("DB_AUDIT_USER"),
        "PASSWORD": env("DB_AUDIT_PASSWORD"),
    },
    # Read/replica-style reporting database.
    "reporting": {
        **_PG,
        "NAME": env("DB_REPORTING_NAME"),
        "USER": env("DB_REPORTING_USER"),
        "PASSWORD": env("DB_REPORTING_PASSWORD"),
    },
}

DATABASE_ROUTERS = ["config.router.AuditAndReportingRouter"]

# Defined before Celery, which needs it. Declared once -- an earlier draft set it
# here via a walrus in CELERY_TIMEZONE and again in the i18n block, which meant the
# Celery value silently ignored DJANGO_TIME_ZONE.
TIME_ZONE = env("DJANGO_TIME_ZONE", default="UTC")

# --------------------------------------------------------------------------
# Cache / Celery
# --------------------------------------------------------------------------
# DB 1. Deliberately `noeviction` on the server: Redis is also the Celery broker,
# so an LRU policy would silently discard queued jobs instead of backpressuring.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": env("CACHE_URL"),
        "KEY_PREFIX": env("CACHE_KEY_PREFIX", default="avanyam"),
    }
}
DJANGO_CELERY_BEAT = True

CELERY_BROKER_URL = env("CELERY_BROKER_URL")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND")
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = TIME_ZONE
CELERY_ENABLE_UTC = True
# Redis has no eviction policy; a lost broker connection should fail loudly.
CELERY_BROKER_TRANSPORT_OPTIONS = {"max_retries": 3}
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = env.int(
    "CELERY_WORKER_PREFETCH_MULTIPLIER", default=1
)

# --------------------------------------------------------------------------
# Object storage -- SeaweedFS via the S3 API
# --------------------------------------------------------------------------
# The app reaches object storage only through S3, so swapping MinIO -> SeaweedFS
# -> anything else is a change to this endpoint alone (memory.md M5).
AWS_ACCESS_KEY_ID = env("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = env("AWS_SECRET_ACCESS_KEY")
AWS_STORAGE_BUCKET_NAME = env("AWS_STORAGE_BUCKET_NAME")
AWS_S3_ENDPOINT_URL = env("AWS_S3_ENDPOINT_URL", default=None)
AWS_S3_REGION_NAME = env("AWS_S3_REGION_NAME", default="us-east-1")
AWS_QUERYSTRING_AUTH = env.bool("AWS_QUERYSTRING_AUTH", default=True)
AWS_QUERYSTRING_EXPIRE = env.int("AWS_QUERYSTRING_EXPIRE", default=900)
AWS_DEFAULT_ACL = env("AWS_DEFAULT_ACL", default="private")
AWS_S3_FILE_OVERWRITE = False
# Presigned URLs must be scoped to this bucket only.
AWS_S3_ADDRESSING_STYLE = "path"
STORAGES = {
    "default": {"BACKEND": "storages.backends.s3.S3Storage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------
# Order is the design (D4): Axes -> OIDC -> LDAP -> ModelBackend.
#
# Axes is first because it is not an identity source -- it is the lockout gate,
# and it only counts a failure if it runs before the backend that would have
# accepted the credentials. Behind any real backend it sees successful logins
# only, so brute-force protection silently does nothing.
#
# `OIDCBackend` is absent because `apps.accounts` does not exist; when it does it
# slots in immediately after Axes, not in place of the others. `ModelBackend`
# stays as the mandatory break-glass path and must never be removed -- it is the
# only way back in if Keycloak and LDAP both fail.
# Custom user: UUID primary key, email as the login identifier, and the
# approval state the whole authorization matrix keys off (§6.2).
AUTH_USER_MODEL = "accounts.User"

AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]

# LDAP is wired only when a directory is actually configured. Adding
# LDAPBackend unconditionally makes every login raise ImproperlyConfigured,
# which is worse than not offering the fallback. See open question A1b: if no
# legacy directory exists, this whole block is dead weight.
if env("AUTH_LDAP_SERVER_URI", default=None):
    # Index 1, not 0: Axes must stay first or lockout stops counting failures.
    AUTHENTICATION_BACKENDS.insert(1, "django_auth_ldap.backend.LDAPBackend")
    import ldap.filter  # noqa: F401  -- proves the python-ldap binding is present

    AUTH_LDAP_SERVER_URI = env("AUTH_LDAP_SERVER_URI")
    AUTH_LDAP_BIND_DN = env("AUTH_LDAP_BIND_DN", default=None)
    AUTH_LDAP_BIND_PASSWORD = env("AUTH_LDAP_BIND_PASSWORD", default=None)
    AUTH_LDAP_BASE_DN = env("AUTH_LDAP_BASE_DN", default=None)
    AUTH_LDAP_USER_SEARCH_FILTER = env(
        "AUTH_LDAP_USER_SEARCH_FILTER", default="(uid=%(user)s)"
    )
    AUTH_LDAP_START_TLS = env.bool("AUTH_LDAP_START_TLS", default=True)
    # Certificate verification stays ON. rules.md §2.3.
    AUTH_LDAP_CONNECTION_OPTIONS = {
        "TLS_REQCERT": "demand",
        "TLS_CACERTFILE": env("AUTH_LDAP_CACERT_FILE", default=None),
    }
    AUTH_LDAP_GROUP_SEARCH_FILTER = env(
        "AUTH_LDAP_GROUP_SEARCH_FILTER", default="(member=%(user_dn)s)"
    )
    AUTH_LDAP_ALWAYS_UPDATE_USERS = env.bool(
        "AUTH_LDAP_ALWAYS_UPDATE_USERS", default=True
    )
    AUTH_LDAP_CACHE_GROUPS = True
    AUTH_LDAP_CACHE_GROUP_KEY = env(
        "AUTH_LDAP_CACHE_GROUP_KEY", default="ldap-groups"
    )
    AUTH_LDAP_CACHE_TIMEOUT = env.int("AUTH_LDAP_CACHE_TIMEOUT", default=300)
    # Fail CLOSED on outage; never fail open. rules.md §2.3.
    AUTH_LDAP_FAIL_ON_ERROR = True

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation."
        "UserAttributeSimilarityValidator"
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Argon2id for every account, signup included (architecture.md §13).
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.ScryptPasswordHasher",
]

# --------------------------------------------------------------------------
# OIDC (Keycloak) -- primary identity path (D2)
# --------------------------------------------------------------------------
OIDC_RP_CLIENT_ID = env("OIDC_RP_CLIENT_ID", default=None)
OIDC_RP_CLIENT_SECRET = env("OIDC_RP_CLIENT_SECRET", default=None)
OIDC_OP_ISSUER_URL = env("OIDC_OP_ISSUER_URL", default=None)
# No client exists in the realm yet, so callback/logout URLs are unset. They are
# derived from the request when absent, which is correct behind a single host.
OIDC_RP_SIGN_ALGO = "RS256"

# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------
# The SMTP *connection* settings live here, not in prod.py. Only the BACKEND
# should differ per environment: an earlier draft put the whole mail config in
# prod.py, so `dev` silently had no mail settings at all and fell back to its
# console backend with an empty host -- which looked like working mail and was
# not. Selecting a real backend from `.env` is then enough to send real email
# from the dev host.
#
# Gmail requires an app password (not the account password) and STARTTLS on
# 587. The app password is 16 characters with NO spaces -- Google displays it in
# four groups of four, and storing it with the spaces makes auth fail with a
# bare 535.
EMAIL_HOST = env("DJANGO_EMAIL_HOST", default="")
EMAIL_PORT = env.int("DJANGO_EMAIL_PORT", default=587)
EMAIL_HOST_USER = env("DJANGO_EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("DJANGO_EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("DJANGO_EMAIL_USE_TLS", default=True)
EMAIL_TIMEOUT = env.int("DJANGO_EMAIL_TIMEOUT", default=20)
# Approved/rejection notices must be traceable to a real mailbox.
DEFAULT_FROM_EMAIL = env("DJANGO_EMAIL_HOST_USER", default="")
SERVER_EMAIL = DEFAULT_FROM_EMAIL

# --------------------------------------------------------------------------
# ClamAV
# --------------------------------------------------------------------------
CLAMAV_HOST = env("CLAMAV_HOST", default="127.0.0.1")
CLAMAV_PORT = env.int("CLAMAV_PORT", default=3310)

# --------------------------------------------------------------------------
# i18n / static
# --------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = env("DJANGO_STATIC_ROOT", default=str(BASE_DIR / "staticfiles"))
STORAGES_STATIC_ROOT = STATIC_ROOT
MEDIA_URL = "media/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --------------------------------------------------------------------------
# DRF / spectacular
# --------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 25,
}
SPECTACULAR_SETTINGS = {"TITLE": "Avanyam LMS API", "VERSION": "0.1.0"}

# --------------------------------------------------------------------------
# Axes -- brute-force lockout
# --------------------------------------------------------------------------
AXES_FAILURE_LIMIT = env.int("AXES_FAILURE_LIMIT", default=5)
AXES_COOLOFF_TIME = env.int("AXES_COOLOFF_TIME", default=60 * 30)
AXES_RESET_ON_SUCCESS = True
# Do not let a locked-out user enumerate accounts by timing.
AXES_ENABLED = True

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
# A function, not a constant, because each environment wants a different default
# and an earlier draft had every env module mutate the imported `LOGGING` dict in
# place -- fragile, and it meant `prod` inherited whatever `dev` had done.
#
# Logging policy lives in `config.logging` (architecture.md §16.1) so it can be
# unit-tested without importing settings.
LOGGING = logging_config(
    env("DJANGO_LOG_LEVEL", default="INFO"),
    sql_debug=env.bool("DJANGO_SQL_DEBUG", default=False),
)

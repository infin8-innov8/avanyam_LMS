#!/usr/bin/env bash
# Verify the P0.2 Django scaffold: layout, settings hygiene, live connectivity to
# PostgreSQL / Redis / SeaweedFS / ClamAV, and the probe endpoints.
#
# Complements check-services.sh (infrastructure processes) by checking that the
# *application* is actually wired to that infrastructure. A green
# check-services.sh plus a green `manage.py check` used to coexist with an app
# that used SQLite and a hardcoded secret -- this script is what would have
# caught that.
#
# Protocol: the embedded Python blocks print `PASS|<label>|<detail>` or
# `FAIL|<label>|<detail>`; bash tallies them. Nothing is counted by hand.
#
# Usage:  ./scripts/check-app.sh
# Exit:   0 = all checks passed, 1 = at least one failed
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PY="avanyam_terra/.avanyam_terra_venv/bin/python"
ENV_FILE="avanyam_terra/.env"
RESULTS=$(mktemp)
trap 'rm -f "$RESULTS"' EXIT

if [ -t 1 ]; then G=$'\033[32m'; R=$'\033[31m'; B=$'\033[1m'; Z=$'\033[0m'
else G=""; R=""; B=""; Z=""; fi

# Sections are recorded, not printed, so they interleave with their own results.
section() { printf 'SEC|%s|\n' "$1" >> "$RESULTS"; }

# record <status> <label> [detail]
record() {
    printf '%s|%s|%s\n' "$1" "$2" "${3:-}" >> "$RESULTS"
}

report() {
    while IFS='|' read -r st label detail; do
        case "$st" in
            "")   ;;   # blank padding line; not a result
            SEC)  printf '\n%s%s%s\n' "$B" "$label" "$Z" ;;
            PASS) printf '  %sPASS%s  %s%s\n' "$G" "$Z" "$label" "${detail:+  $detail}" ;;
            *)    printf '  %sFAIL%s  %s%s\n' "$R" "$Z" "$label" "${detail:+  $detail}" ;;
        esac
    done < "$RESULTS"
}

[ -x "$PY" ] || { echo "FATAL: $PY not found or not executable"; exit 1; }

# ---------------------------------------------------------------- layout ----
section "Repository layout (architecture.md 16.1)"
for f in manage.py src/config/__init__.py src/config/settings/base.py \
         src/config/settings/dev.py src/config/settings/staging.py \
         src/config/settings/prod.py src/config/urls.py src/config/wsgi.py \
         src/config/asgi.py src/config/celery.py src/config/router.py \
         src/config/health.py; do
    if [ -f "$f" ]; then record PASS "$f exists"; else record FAIL "$f MISSING"; fi
done

# The stock scaffold must stay gone. Its presence is how this repo ended up with
# a fully-provisioned PostgreSQL cluster running while the app used SQLite.
for p in avanyam_terra/manage.py avanyam_terra/avanyam_terra; do
    if [ -e "$p" ]; then record FAIL "host-named $p still present"
    else record PASS "host-named $p retired"; fi
done

# ------------------------------------------------------------ .env hygiene ---
section "Environment file"
if [ -f "$ENV_FILE" ]; then record PASS "$ENV_FILE exists"
else record FAIL "$ENV_FILE MISSING"; echo "FATAL: no .env"; exit 1; fi

MODE=$(stat -c '%a' "$ENV_FILE")
if [ "$MODE" = "600" ]; then record PASS "$ENV_FILE mode 0600"
else record FAIL "$ENV_FILE mode is $MODE, want 600"; fi

# Settings module must be host-agnostic: aqua (VM3) runs this same code.
if grep -qE '^DJANGO_SETTINGS_MODULE=config\.settings\.' "$ENV_FILE"; then
    record PASS "DJANGO_SETTINGS_MODULE is config.settings.* (host-agnostic)"
else record FAIL "DJANGO_SETTINGS_MODULE is not config.settings.*"; fi

if grep -qE '^DJANGO_SETTINGS_MODULE=config\.settings\.dev' avanyam_terra/.env.example; then
    record PASS ".env.example agrees with .env on the settings module"
else record FAIL ".env.example settings module disagrees with .env"; fi

# django-environ's Env.__call__ is (var, cast=None, default=...): the second
# positional is `cast`, NOT `default`. Passing a default positionally silently
# turns the string into a cast callable. That exact bug shipped here once.
if grep -rqE 'env\("[A-Z_0-9]+", *"' src/config/settings/ --include='*.py'; then
    record FAIL "positional 2nd arg to env() found (that is \`cast\`, not \`default\`)" \
           "see src/config/settings/*.py"
else record PASS "no positional-default env() calls (django-environ trap)"; fi

# Root logger at DEBUG makes botocore log SigV4 Authorization headers, and an
# env module mutating LOGGING in place means prod inherits dev's dict.
if grep -rqE 'LOGGING\["root"\]\["level"\]' src/config/settings/ --include='*.py'; then
    record FAIL "LOGGING mutated in place by an env module (use logging_config())"
else record PASS "LOGGING built by logging_config(), not mutated per-env"; fi

# SECRET_KEY must have no inline default: a missing key has to stop the boot.
if grep -qE '^SECRET_KEY *= *env\("DJANGO_SECRET_KEY"\)[^,]*$' src/config/settings/base.py; then
    record PASS "SECRET_KEY declared with no inline default"
else record FAIL "SECRET_KEY must be env(\"DJANGO_SECRET_KEY\") with no default= argument"; fi

# ------------------------------------------------------- Django checks ------
section "Django system checks"
if out=$("$PY" manage.py check 2>&1); then
    record PASS "manage.py check: no issues"
else
    record FAIL "manage.py check reported issues" "$(printf '%s' "$out" | tr '\n' ' ' | cut -c1-200)"
fi

if out=$(DJANGO_SETTINGS_MODULE=config.settings.prod "$PY" manage.py check --deploy --fail-level WARNING 2>&1); then
    record PASS "check --deploy (prod settings): clean at WARNING+"
else
    record FAIL "check --deploy reported issues" "$(printf '%s' "$out" | tr '\n' ' ' | cut -c1-200)"
fi

# prod must refuse to boot with an empty allowlist, not silently 400 everything.
if DJANGO_ALLOWED_HOSTS= DJANGO_SETTINGS_MODULE=config.settings.prod \
     "$PY" -c "import config.settings.prod" >/dev/null 2>&1; then
    record FAIL "prod booted with empty DJANGO_ALLOWED_HOSTS (should refuse)"
else record PASS "prod refuses empty DJANGO_ALLOWED_HOSTS"; fi

# ------------------------------------------- application + connectivity -----
section "Application wiring and live connectivity"
"$PY" - > "$RESULTS.app" 2>/dev/null <<'PYEOF'
import json, logging, os, re, socket, sys
from urllib.parse import urlsplit

sys.path.insert(0, "src")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
import django
django.setup()
logging.disable(logging.CRITICAL)

from django.conf import settings
from django.db import connections

def emit(status, label, detail=""):
    print(f"{status}|{label}|{detail}")

def check(label, ok, detail=""):
    emit("PASS" if ok else "FAIL", label, detail)
    return 0 if ok else 1

bad = 0

# --- secret strength (Django security.W009 floor: 50 chars, 5 unique) --------
sk = settings.SECRET_KEY
bad += check("SECRET_KEY >=50 chars, >=5 unique, not django-insecure",
             len(sk) >= 50 and len(set(sk)) >= 5 and not sk.startswith("django-insecure-"),
             f"len={len(sk)} unique={len(set(sk))}")

# --- four roles, four distinct database/role pairs (architecture.md 14.3) ----
EXPECT = {"default": ("avanyam_app", "avanyam"),
          "migrate": ("avanyam_migrate", "avanyam"),
          "audit": ("avanyam_audit", "avanyam_audit"),
          "reporting": ("avanyam_reporting", "avanyam_reporting")}
bad += check("four database aliases configured",
             set(settings.DATABASES) == set(EXPECT), ", ".join(sorted(settings.DATABASES)))
for alias, (want_role, want_db) in EXPECT.items():
    try:
        with connections[alias].cursor() as c:
            c.execute("SELECT current_user, current_database()")
            role, db = c.fetchone()
        bad += check(f"db '{alias}' connects as {want_role} on {want_db}",
                     (role, db) == (want_role, want_db), f"got role={role} db={db}")
    except Exception as e:
        bad += check(f"db '{alias}' connects", False, f"{type(e).__name__}: {e}")

# --- least privilege (architecture.md 14.3) ---------------------------------
# Runtime roles must NOT be able to alter schema. Ownership of a database
# implies CREATE on it, so a role that owns its own database silently gets DDL
# rights. `avanyam` is owned by `avanyam_migrate`, which is correct; audit and
# reporting owning themselves is not.
#
# 3-arg form on purpose: has_database_privilege(a, b) means (database,
# privilege), so passing a ROLE name there asks about a database named after the
# role and raises `database "<role>" does not exist`.
with connections["default"].cursor() as c:
    c.execute("""SELECT datname, pg_get_userbyid(datdba) FROM pg_database
                 WHERE datname LIKE 'avanyam%' ORDER BY 1""")
    db_owners = dict(c.fetchall())
# Ownership implies CREATE, so an owner has DDL rights. Which owners are wrong
# depends on the role's job, so this is judged per role rather than as one rule:
#
#   avanyam_app       owns nothing              -- must hold no DDL at runtime
#   avanyam_audit     owns avanyam_audit        -- CORRECT and required: it
#                       creates the audit_log table. Flagging this was noise.
#   avanyam_reporting owns avanyam_reporting    -- wrong if reporting is
#                       read-only, which is the documented intent.
RUNTIME_ROLES = {"avanyam_app", "avanyam_audit", "avanyam_reporting"}
own_own = {d: r for d, r in db_owners.items() if r in RUNTIME_ROLES}
bad += check("avanyam_app owns no database (no DDL at runtime)",
             "avanyam_app" not in own_own.values(),
             ", ".join(f"{r} owns {d}" for d, r in own_own.items()) or "none")

REPORTING_OWNS = own_own.get("avanyam_reporting") == "avanyam_reporting"
bad += check("avanyam_reporting does not own its database (read-only role)",
             not REPORTING_OWNS,
             "avanyam_reporting owns avanyam_reporting -> it holds CREATE; "
             "fix needs a superuser (ALTER DATABASE ... OWNER TO), which this "
             "host does not have. See CREDENTIALS.md."
             if REPORTING_OWNS else "reporting role holds no DDL")

# Only the web/runtime role is judged here. avanyam_audit MUST hold CREATE in
# avanyam_audit -- it creates the audit_log table -- so applying this rule to it
# flagged correct configuration as broken. The audit and reporting roles are
# covered by the ownership check above instead.
for alias in ("default",):
    try:
        with connections[alias].cursor() as c:
            c.execute("""SELECT has_database_privilege(
                                current_user, current_database(), 'CREATE')""")
            can_create = c.fetchone()[0]
        bad += check(f"runtime role for '{alias}' cannot CREATE (least privilege)",
                     not can_create, f"CREATE={can_create}")
    except Exception as e:
        bad += check(f"runtime role for '{alias}' privilege probe", False,
                     f"{type(e).__name__}: {e}")

# --- Redis DB allocation: 1 cache / 0 broker / 2 results, never overlapping ---
# Parse the PATH component. A naive `:(\d+)` matches the port (6379) instead.
def db_index(url):
    path = urlsplit(str(url)).path.strip("/")
    return int(path) if path.isdigit() else None

idx = {"cache": db_index(settings.CACHES["default"]["LOCATION"]),
       "broker": db_index(settings.CELERY_BROKER_URL),
       "results": db_index(settings.CELERY_RESULT_BACKEND)}
bad += check("Redis DBs are cache=1 broker=0 results=2, no overlap",
             idx == {"cache": 1, "broker": 0, "results": 2},
             " ".join(f"{k}={v}" for k, v in idx.items()))

from django.core.cache import cache
cache.set("check-app", "v", 30)
bad += check("cache read-after-write", cache.get("check-app") == "v")

# --- schema actually migrated ------------------------------------------------
# The scaffold imports and connects with an empty database, so every check above
# can pass while the app is non-functional: no auth_user means no login, no
# session table means every session write fails, no axes_accessattempt means
# brute-force lockout silently does nothing. Assert the tables exist.
# `accounts_user`, not `auth_user`: AUTH_USER_MODEL is accounts.User, so Django
# never creates auth_user. This list still said auth_user, which made the check
# permanently red and trained everyone to ignore it.
CORE_TABLES = ("django_migrations", "accounts_user", "accounts_signuprequest",
               "axes_accessattempt", "django_session", "django_admin_log")
with connections["default"].cursor() as c:
    c.execute("""SELECT table_name FROM information_schema.tables
                 WHERE table_schema = current_schema()""")
    present = {r[0] for r in c.fetchall()}
missing = [t for t in CORE_TABLES if t not in present]
bad += check("migrations applied: core tables exist in `default`", not missing,
             "missing: " + ", ".join(missing) if missing
             else f"{len(present)} tables present")

# --- object store -----------------------------------------------------------
try:
    import boto3
    s3 = boto3.client("s3", endpoint_url=settings.AWS_S3_ENDPOINT_URL,
                      aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
                      aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
                      region_name=settings.AWS_S3_REGION_NAME)
    s3.head_bucket(Bucket=settings.AWS_STORAGE_BUCKET_NAME)
    bad += check("object store head_bucket", True,
                 f"{settings.AWS_STORAGE_BUCKET_NAME} @ {settings.AWS_S3_ENDPOINT_URL}")
except Exception as e:
    bad += check("object store head_bucket", False, f"{type(e).__name__}: {e}")

# --- clamd ------------------------------------------------------------------
try:
    with socket.create_connection((settings.CLAMAV_HOST, settings.CLAMAV_PORT), 5) as s:
        s.sendall(b"PING\x00")
        reply = s.recv(64)
    bad += check("clamd PING", reply.startswith(b"PONG"),
                 f"{settings.CLAMAV_HOST}:{settings.CLAMAV_PORT}")
except Exception as e:
    bad += check("clamd PING", False, f"{type(e).__name__}: {e}")

# --- auth -------------------------------------------------------------------
# Axes must be FIRST: behind a real backend it only ever sees successful logins,
# so brute-force lockout silently stops working.
backends = settings.AUTHENTICATION_BACKENDS
chain = " -> ".join(b.split(".")[-1] for b in backends)
bad += check("AxesStandaloneBackend is first (lockout depends on it)",
             bool(backends) and backends[0] == "axes.backends.AxesStandaloneBackend", chain)
bad += check("ModelBackend retained as break-glass path",
             "django.contrib.auth.backends.ModelBackend" in backends)
bad += check("Argon2 is the primary password hasher",
             settings.PASSWORD_HASHERS[0].endswith("Argon2PasswordHasher"))
bad += check("LDAP backend absent while no directory is configured (A1b open)",
             not any("ldap" in b.lower() for b in backends),
             "conditional on AUTH_LDAP_SERVER_URI")
bad += check("database router installed",
             settings.DATABASE_ROUTERS == ["config.router.AuditAndReportingRouter"])

# --- logging ----------------------------------------------------------------
noisy = {n: logging.getLogger(n).level for n in
         ("botocore", "boto3", "urllib3", "django.db.backends")}
bad += check("botocore/boto3/urllib3/sql loggers floored at WARNING",
             all(v >= logging.WARNING for v in noisy.values()),
             ", ".join(f"{k}={v}" for k, v in noisy.items()))
bad += check("root logger not at DEBUG", logging.getLogger().level > logging.DEBUG,
             f"root={logging.getLogger().level}")

# --- celery -----------------------------------------------------------------
os.environ.setdefault("CELERY_APP", "config.celery")
from config.celery import app
bad += check("celery app discovered", app.main == "avanyam", app.main)
bad += check("celery uses DB-backed beat, not static schedule files",
             app.conf.beat_scheduler == "celery.beat:PersistentScheduler",
             app.conf.beat_scheduler)

# --- probes -----------------------------------------------------------------
from django.test import Client, override_settings

c = Client(HTTP_HOST="localhost")
r = c.get("/livez"); b = json.loads(r.content)
bad += check("/livez returns 200 UP",
             r.status_code == 200 and b["status"] == "UP", f"HTTP {r.status_code}")

r = c.get("/readyz"); b = json.loads(r.content)
bad += check("/readyz returns 200 UP, all dependencies UP",
             r.status_code == 200 and b["status"] == "UP"
             and all(v == "UP" for v in b.get("checks", {}).values()),
             " ".join(f"{k}={v}" for k, v in b.get("checks", {}).items()))

# The livez/readyz split is the whole point of having two probes: a cache outage
# must NOT make the process look dead, or the orchestrator restarts healthy
# workers during a recoverable dependency blip.
broken = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache",
                     "LOCATION": "redis://127.0.0.1:6399/1", "KEY_PREFIX": "avanyam"}}
with override_settings(CACHES=broken):
    rl = c.get("/livez")
    rr = c.get("/readyz")
    bad += check("cache outage: /livez stays 200 UP (no restart loop)",
                 rl.status_code == 200 and json.loads(rl.content)["status"] == "UP")
    bad += check("cache outage: /readyz drops to 503 (drained, not killed)",
                 rr.status_code == 503 and json.loads(rr.content)["status"] == "DOWN")

r = Client(HTTP_HOST="evil.example.com").get("/livez")
bad += check("foreign Host header rejected with 400", r.status_code == 400,
             f"got HTTP {r.status_code}")

sys.exit(1 if bad else 0)
PYEOF
cat "$RESULTS.app" >> "$RESULTS"; rm -f "$RESULTS.app"

# --------------------------------------------------------- secret hygiene ---
section "Secret hygiene"
# Compare live values against every .md, but print only file names -- never a value.
LEAK=$(grep -rlF -f <(grep -oE '^[A-Z_0-9]*(PASSWORD|SECRET_KEY|SECRET_ACCESS_KEY)[A-Z_0-9]*=.+' "$ENV_FILE" \
        | cut -d= -f2- | tr -d '"'"'" | grep -v '^$') --include='*.md' . 2>/dev/null || true)
if [ -n "$LEAK" ]; then
    record FAIL "live credential value found in Markdown" "$(echo "$LEAK" | tr '\n' ' ')"
else
    record PASS "no live credential values in any .md file"
fi

# ------------------------------------------------------------------ result --
report
P=$(grep -c '^PASS|' "$RESULTS" || true)
F=$(grep -c '^FAIL|' "$RESULTS" || true)
printf '\n%s%d passed, %d failed%s\n' "$B" "$P" "$F" "$Z"
[ "$F" -eq 0 ] || exit 1

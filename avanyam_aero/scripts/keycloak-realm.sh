#!/usr/bin/env bash
# Operate the local Keycloak realm: environment, lifecycle, import, verification.
#
#   ./scripts/keycloak-realm.sh env             # generate run/keycloak.env from .env
#   ./scripts/keycloak-realm.sh start           # start bound to loopback only
#   ./scripts/keycloak-realm.sh stop
#   ./scripts/keycloak-realm.sh restart
#   ./scripts/keycloak-realm.sh status
#   ./scripts/keycloak-realm.sh bootstrap-admin # one-off; server MUST be stopped
#   ./scripts/keycloak-realm.sh import          # start with --import-realm (fresh DB)
#   ./scripts/keycloak-realm.sh verify          # realm/roles/groups/default role
#   ./scripts/keycloak-realm.sh export          # rewrite the fixture from the server
#   ./scripts/keycloak-realm.sh check-fixture   # prove the fixture really imports
#
# Why this script exists (K2). Every default below was chosen because the
# obvious alternative fails, and each failure mode is silent or misleading:
#
# 1. NETWORK. We use --network host so Keycloak reaches PostgreSQL on the
#    laptop's own 127.0.0.1:5432. With host networking the JDBC URL MUST be
#    127.0.0.1. The bridge-network spelling host.docker.internal only resolves
#    when --add-host host.docker.internal:host-gateway is also passed; without
#    that flag the container exits ~90s in with
#        SQLState 08001 / The connection attempt failed
#    which reads like a database problem rather than a name-resolution one.
#
# 2. BIND ADDRESS. We pass --http-host 127.0.0.1. Setting QUARKUS_HTTP_HOST in
#    the environment does NOT work: the server logs "Listening on:
#    http://0.0.0.0:8080" and publishes to every interface, breaking the
#    loopback-only invariant (K7). Always confirm with `ss -tln`, never by
#    trusting the env var.
#
# 3. HEALTH lives on the management port, not the HTTP port. With
#    KC_HEALTH_ENABLED=true, /health/ready answers on 9000; asking 8080 returns
#    {"error":"Unable to find matching target resource method"}.
#
# 4. ADMIN. Keycloak 26 creates no admin user on its own, so kcadm fails with
#    "Invalid user credentials [invalid_grant]" against a perfectly healthy
#    server. KC_BOOTSTRAP_ADMIN_USERNAME/PASSWORD are honoured ONLY on the
#    first startup that creates the master realm. Adding them to an existing
#    database changes nothing, which is why `bootstrap-admin` exists as the
#    repair path.
#
# 5. bootstrap-admin is CREATE-ONLY ("user with username exists") and needs
#    port 9000 free, so it must run with the server stopped. Hence `stop` is a
#    hard prerequisite here, and the check is enforced rather than assumed.
#
# 6. DEFAULT ROLES are not a settable field. `kcadm.sh update realms/avanyam
#    -s 'defaultRoles=[...]'` exits 0, prints nothing, and changes nothing.
#    The real mechanism is the default-roles-<realm> composite role, mutated
#    through POST /admin/realms/{realm}/roles-by-id/{id}/composites.
#    `verify` therefore checks observable behaviour (what a brand-new user
#    actually receives) instead of trusting the configured field.
#
# 7. ROLE TABLES were renamed in KC26: realm_role -> keycloak_role, and
#    default_realm_role no longer exists. Querying the old names returns zero
#    rows and looks like data loss. This script verifies via the REST API only.
#
# Secrets are read from the gitignored, mode-600 avanyam_aero/.env and written
# to the gitignored runtime file avanyam_aero/run/keycloak.env (mode 600).
# Nothing here prints a password or commits one.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AERO="$(cd "$HERE/.." && pwd)"
ROOT="$(cd "$AERO/.." && pwd)"

ENV_FILE="$AERO/.env"
RUN_DIR="$AERO/run"
RUNTIME_ENV="$RUN_DIR/keycloak.env"
FIXTURE="$AERO/conf/keycloak/avanyam-realm.json"

CONTAINER="avanyam-keycloak"
# Digest-pinned (K10): a mutable tag can change under you mid-incident.
IMAGE="quay.io/keycloak/keycloak@sha256:09a381c715ab0b111835b70f2905955274843a219c6f27efb348e4d9f4086858"

REALM="avanyam"
HTTP_HOST="127.0.0.1"
HTTP_PORT="8080"
MGMT_PORT="9000"
DB_HOST="127.0.0.1"
DB_PORT="5432"
DB_NAME="keycloak"
DB_USER="keycloak"

# Keycloak needs ~90-250s to migrate its schema and bind; polling beats sleep.
READY_TIMEOUT="${READY_TIMEOUT:-300}"

# Docker may need sudo. Detect once instead of assuming -- and fail with an
# actionable message rather than letting `sudo` print "a terminal is required".
if docker ps >/dev/null 2>&1; then
  DOCKER=(docker)
elif sudo -n docker ps >/dev/null 2>&1; then
  DOCKER=(sudo -n docker)
elif [ "$(id -u)" = 0 ]; then
  DOCKER=(docker)
else
  cat >&2 <<'MSG'
ERROR: docker needs sudo here and cannot prompt for a password.

  This script is not setuid and cannot supply your sudo password. Either:

    1. run the whole script under sudo:
         sudo -E ./scripts/keycloak-realm.sh verify
       (-E keeps your PATH; the script reads secrets from .env as the
        invoking user, so prefer option 2 if file ownership matters)

    2. grant passwordless docker for your user (recommended for a dev laptop):
         sudo usermod -aG docker "$USER"    # then log out and back in

  Do NOT work around this by putting a password in this script.
MSG
  exit 1
fi

say()  { printf '  %s\n' "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# Read a key from the gitignored .env without printing it.
env_get() {
  local k="$1" f="${2:-$ENV_FILE}"
  [ -f "$f" ] || die "missing $f"
  local v
  v="$(grep -E "^${k}=" "$f" | tail -1 | cut -d= -f2-)" || true
  [ -n "$v" ] || die "$k not set in $(basename "$f")"
  printf '%s' "$v"
}

need_env() { [ -f "$RUNTIME_ENV" ] || die "no $RUNTIME_ENV -- run: $0 env"; }

# Wait for the management health endpoint, and report *why* if it never comes.
wait_ready() {
  local i=0 st
  while [ "$i" -lt "$READY_TIMEOUT" ]; do
    if curl -sf -o /dev/null "http://${HTTP_HOST}:${MGMT_PORT}/health/ready" 2>/dev/null; then
      say "ready after ~$((i + 1))s"; return 0
    fi
    st="$("${DOCKER[@]}" inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo gone)"
    if [ "$st" != running ]; then
      say "container '$st' -- last log lines:"
      "${DOCKER[@]}" logs --tail 15 "$CONTAINER" 2>&1 | sed 's/^/    /' || true
      return 1
    fi
    sleep 5; i=$((i + 5))
  done
  say "timed out after ${READY_TIMEOUT}s"
  return 1
}

cmd_env() {
  command -v python3 >/dev/null || die "python3 required"
  mkdir -p "$RUN_DIR"; chmod 700 "$RUN_DIR"
  umask 077
  python3 - "$ENV_FILE" "$RUNTIME_ENV" <<'PY'
import os, re, stat, sys
src, dst = sys.argv[1], sys.argv[2]
s = open(src).read()
def g(k):
    m = re.search(r'(?m)^%s=(.*)$' % re.escape(k), s)
    if not m or not m.group(1).strip():
        sys.exit("ERROR: %s not set in %s" % (k, os.path.basename(src)))
    return m.group(1).strip()

lines = [
    "# GENERATED by scripts/keycloak-realm.sh -- do not edit, do not commit.",
    "# gitignored via the run/.* rule; mode 600.",
    "KC_DB=postgres",
    # 127.0.0.1, NOT host.docker.internal: we run with --network host (trap 1).
    "KC_DB_URL=jdbc:postgresql://127.0.0.1:5432/%s" % g("KEYCLOAK_DB_NAME") if "KEYCLOAK_DB_NAME" in s
    else "KC_DB_URL=jdbc:postgresql://127.0.0.1:5432/keycloak",
    "KC_DB_USERNAME=%s" % g("KEYCLOAK_DB_USER"),
    "KC_DB_PASSWORD=%s" % g("KEYCLOAK_DB_PASSWORD"),
    "KC_HOSTNAME=http://127.0.0.1:8080",
    "KC_HTTP_ENABLED=true",
    "KC_HEALTH_ENABLED=true",   # health answers on the management port (trap 3)
    "KC_LOG_LEVEL=INFO",
    "KC_BOOTSTRAP_ADMIN_USERNAME=%s" % g("KC_BOOTSTRAP_ADMIN_USERNAME"),
    "KC_BOOTSTRAP_ADMIN_PASSWORD=%s" % g("KC_BOOTSTRAP_ADMIN_PASSWORD"),
]
open(dst, "w").write("\n".join(lines) + "\n")
os.chmod(dst, stat.S_IRUSR | stat.S_IWUSR)
print("  wrote %s (mode 600)" % dst)
PY
  say "db=127.0.0.1:${DB_PORT}/${DB_NAME}  http=${HTTP_HOST}:${HTTP_PORT}  mgmt=${HTTP_HOST}:${MGMT_PORT}"
  say "realm fixture: ${FIXTURE#"$ROOT"/}"
}

cmd_start() {
  need_env
  "${DOCKER[@]}" rm -f "$CONTAINER" >/dev/null 2>&1 || true
  say "starting $CONTAINER (host network, loopback-bound)"
  # --restart unless-stopped: Keycloak is the PRIMARY identity path (D2), and
  # without a policy any SIGTERM -- including the one systemd sends on reboot --
  # leaves it down until a human runs this script. That actually happened: the
  # container sat Exited(143) while SeaweedFS, which Compose gives a policy to,
  # came back on its own. `unless-stopped` rather than `always` so a deliberate
  # `docker stop` for maintenance is respected when the daemon restarts.
  "${DOCKER[@]}" run -d --name "$CONTAINER" \
    --restart unless-stopped \
    --network host \
    --env-file "$RUNTIME_ENV" \
    -v "${FIXTURE}:/opt/keycloak/data/import/avanyam-realm.json:ro" \
    "$IMAGE" start-dev --http-host "$HTTP_HOST" >/dev/null
  wait_ready
  cmd_status
}

cmd_import() {
  need_env
  "${DOCKER[@]}" rm -f "$CONTAINER" >/dev/null 2>&1 || true
  say "starting with --import-realm (use on a fresh database)"
  "${DOCKER[@]}" run -d --name "$CONTAINER" \
    --network host \
    --env-file "$RUNTIME_ENV" \
    -v "${FIXTURE}:/opt/keycloak/data/import/avanyam-realm.json:ro" \
    "$IMAGE" start-dev --import-realm --http-host "$HTTP_HOST" >/dev/null
  wait_ready || true
  "${DOCKER[@]}" logs "$CONTAINER" 2>&1 | grep -iE "imported|Unrecognized|Failed to run import" | tail -5 | sed 's/^/  /' || true
  cmd_verify
}

cmd_stop()   { "${DOCKER[@]}" rm -f "$CONTAINER" >/dev/null 2>&1 && say "removed $CONTAINER" || say "not running"; }
cmd_restart(){ cmd_stop; cmd_start; }

cmd_status() {
  local st health binds
  st="$("${DOCKER[@]}" inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo absent)"
  health="$(curl -sf "http://${HTTP_HOST}:${MGMT_PORT}/health/ready" 2>/dev/null | tr -d ' \n' || echo unreachable)"
  say "container : $st"
  say "health    : ${health:-unreachable}"
  binds="$(ss -tln 2>/dev/null | grep -E ":(${HTTP_PORT}|${MGMT_PORT})\b" | awk '{print $4}' | sort -u | tr '\n' ' ')"
  say "listening : ${binds:-none}"
  case "$binds" in
    *0.0.0.0*|*\[::\]*) die "LEAK: Keycloak is bound to a wildcard address (trap 2)" ;;
  esac
  say "realm     : HTTP $(curl -sf -o /dev/null -w '%{http_code}' "http://${HTTP_HOST}:${HTTP_PORT}/realms/${REALM}" 2>/dev/null || echo '-') at /realms/${REALM} (200 = realm exists and is public)"
}

# Admin token via the master realm; printed nowhere, kept only in a variable.
admin_token() {
  local u p resp
  u="$(grep -E '^KC_BOOTSTRAP_ADMIN_USERNAME=' "$RUNTIME_ENV" | cut -d= -f2- || true)"
  p="$(grep -E '^KC_BOOTSTRAP_ADMIN_PASSWORD=' "$RUNTIME_ENV" | cut -d= -f2- || true)"
  if [ -z "$u" ] || [ -z "$p" ]; then
    printf ''   # caller turns this into the "run bootstrap-admin" message
    return 0
  fi
  resp="$(curl -sS -d "client_id=admin-cli" -d "username=$u" -d "password=$p" \
    -d "grant_type=password" \
    "http://${HTTP_HOST}:${HTTP_PORT}/realms/master/protocol/openid-connect/token" \
    2>/dev/null || true)"
  # A failed grant returns {"error":...}, not a token; never let that reach json.load.
  printf '%s' "$resp" | python3 -c "
import json,sys
try:
    print(json.load(sys.stdin).get('access_token',''))
except Exception:
    print('')
" 2>/dev/null || printf ''
}

cmd_verify() {
  need_env
  local tok; tok="$(admin_token)"
  [ -n "$tok" ] || die "cannot authenticate -- run '$0 bootstrap-admin' (traps 4 and 5)"
  local T="$tok" R="$REALM" H="$HTTP_HOST" P="$HTTP_PORT" py
  py='
import json,sys,urllib.request
T,R,H,P=sys.argv[1:5]
def get(p):
    r=urllib.request.Request("http://%s:%s/admin/realms/%s%s"%(H,P,R,p),
                             headers={"Authorization":"Bearer "+T})
    return json.load(urllib.request.urlopen(r))
try:
    d=get("")
except Exception as e:
    sys.exit("ERROR: realm %s unreachable: %s"%(R,e))
print("  realm        : %s (enabled=%s)"%(d["realm"],d["enabled"]))
print("  displayName  : %s"%d.get("displayName"))
print("  sslRequired  : %s   accessTokenLifespan=%ss"%(d.get("sslRequired"),d.get("accessTokenLifespan")))
print("  bruteForce   : %s (factor %s)"%(d.get("bruteForceProtected"),d.get("failureFactor")))
want={"TRAINEE","TRAINER","ADMIN"}
roles={r["name"] for r in get("/roles")}
print("  spec roles   : %s  %s"%(sorted(want&roles),"OK" if want<=roles else "MISSING %s"%sorted(want-roles)))
groups={g["name"] for g in get("/groups")}
wgroups={"avanyam-trainees","avanyam-trainers","avanyam-admins"}
print("  spec groups  : %s  %s"%(sorted(wgroups&groups),"OK" if wgroups<=groups else "MISSING %s"%sorted(wgroups-groups)))
builtin={"account","account-console","admin-cli","broker","realm-management","security-admin-console"}
custom=[c["clientId"] for c in get("/clients") if c["clientId"] not in builtin]
print("  app clients  : %s"%(", ".join(custom) if custom else "none (correct -- deferred to app day)"))
# Default role is a composite, not a field (trap 6) -- assert on behaviour.
comp=get("/roles/default-roles-%s"%R)
eff={c["name"] for c in get("/roles-by-id/%s/composites"%comp["id"])}
print("  default role : TRAINEE granted=%s  TRAINER/ADMIN leaked=%s"%(
    "TRAINEE" in eff, bool({"TRAINER","ADMIN"}&eff)))
'
  python3 -c "$py" "$T" "$R" "$H" "$P"
}

cmd_export() {
  need_env
  local tok; tok="$(admin_token)"
  [ -n "$tok" ] || die "cannot authenticate -- run '$0 bootstrap-admin'"
  local T="$tok" R="$REALM" H="$HTTP_HOST" P="$HTTP_PORT"
  python3 - "$T" "$R" "$H" "$P" "$FIXTURE" <<'PY'
import json,sys,urllib.request,collections
T,R,H,P,out=sys.argv[1:6]
def get(p):
    r=urllib.request.Request("http://%s:%s/admin/realms/%s%s"%(H,P,R,p),
                             headers={"Authorization":"Bearer "+T})
    return json.load(urllib.request.urlopen(r))
realm=get(""); roles=get("/roles"); groups=get("/groups")

CUSTOM={"TRAINEE","TRAINER","ADMIN"}
# Strip every server-assigned identifier. 'id' collides on re-import (trap 2);
# 'containerId' is equally volatile -- it points at the live realm row and means
# nothing in a fresh database. Both must go, and the guard below checks both.
VOLATILE={"id","attributes","containerId"}
def strip(o):
    if isinstance(o,dict):
        return {k:strip(v) for k,v in o.items() if k not in VOLATILE}
    if isinstance(o,list):
        return [strip(v) for v in o]
    return o

fx=strip(realm)
# defaultRoles is the import representation of the default set; the exported
# 'defaultRole' object is a *response* artefact and is dropped outright.
fx.pop("defaultRole",None)
fx["roles"]={"realm":[strip(r) for r in roles if r["name"] in CUSTOM]}
fx["groups"]=[strip(g) for g in groups]
fx["defaultRoles"]=["offline_access","uma_authorization","TRAINEE"]
fx["clients"]=[]; fx["users"]=[]

# Hard guard: any surviving server-assigned id makes the fixture
# non-re-importable or silently realm-specific (trap 2).
VOL={"id","containerId"}
def ids(o):
    if isinstance(o,dict):
        for k,v in o.items():
            if k in VOL: yield k
            yield from ids(v)
    elif isinstance(o,list):
        for v in o: yield from ids(v)
left=list(ids(fx))
if left:
    sys.exit("ERROR: %d volatile field(s) left in fixture (%s); it would not re-import cleanly"
             %(len(left),", ".join(sorted(set(left)))))
if "defaultRole" in fx:
    sys.exit("ERROR: defaultRole survived export; it embeds a stale role reference")

with open(out,"w") as f:
    json.dump(fx,f,indent=2); f.write("\n")
print("  wrote %s"%out)
print("  roles : %s"%[r["name"] for r in fx["roles"]["realm"]])
print("  groups: %s"%[g["name"] for g in fx["groups"]])
print("  explicit ids: none (re-importable)")
PY
  say "now prove it: $0 check-fixture"
}

# Import the fixture under a throwaway realm name, read it back, then delete it.
cmd_check_fixture() {
  need_env
  local scratch="zz-fixture-check" tok tdir
  tdir="$(mktemp -d)"; chmod 700 "$tdir"
  python3 - "$FIXTURE" "$tdir/check.json" "$scratch" <<'PY'
import json,sys
d=json.load(open(sys.argv[1])); d["realm"]=sys.argv[3]
json.dump(d,open(sys.argv[2],"w"),indent=2)
PY
  "${DOCKER[@]}" rm -f "$CONTAINER" >/dev/null 2>&1 || true
  say "importing fixture as realm '${scratch}'"
  "${DOCKER[@]}" run -d --name "$CONTAINER" --network host \
    --env-file "$RUNTIME_ENV" \
    -v "${tdir}/check.json:/opt/keycloak/data/import/check.json:ro" \
    "$IMAGE" start-dev --import-realm --http-host "$HTTP_HOST" >/dev/null
  wait_ready >/dev/null || true
  "${DOCKER[@]}" logs "$CONTAINER" 2>&1 | grep -iE "imported|Unrecognized|Failed to run import" | tail -3 | sed 's/^/  /'
  tok="$(admin_token)"
  [ -n "$tok" ] || die "cannot authenticate after import"
  python3 - "$tok" "$scratch" "$HTTP_HOST" "$HTTP_PORT" <<'PY'
import json,sys,urllib.request
T,R,H,P=sys.argv[1:5]
def get(p,realm=R):
    r=urllib.request.Request("http://%s:%s/admin/realms/%s%s"%(H,P,realm,p),
                             headers={"Authorization":"Bearer "+T})
    return json.load(urllib.request.urlopen(r))
def delete(p,realm=R):
    r=urllib.request.Request("http://%s:%s/admin/realms/%s%s"%(H,P,realm,p),
                             headers={"Authorization":"Bearer "+T},method="DELETE")
    urllib.request.urlopen(r)
try:
    roles={r["name"] for r in get("/roles")}
    groups={g["name"] for g in get("/groups")}
    comp=get("/roles/default-roles-%s"%R)
    eff={c["name"] for c in get("/roles-by-id/%s/composites"%comp["id"])}
    want={"TRAINEE","TRAINER","ADMIN"}
    wg={"avanyam-trainees","avanyam-trainers","avanyam-admins"}
    ok = want<=roles and wg<=groups and "TRAINEE" in eff
    print("  roles        : %s"%sorted(want&roles))
    print("  groups       : %s"%sorted(wg&groups))
    print("  TRAINEE default: %s"%("TRAINEE" in eff))
    print("  FIXTURE %s"%("VALID" if ok else "INVALID"))
finally:
    try:
        delete(""); print("  scratch realm removed")
    except Exception as e:
        print("  WARNING: could not delete scratch realm: %s"%e)
PY
  rm -rf "$tdir"
  cmd_restart
}

cmd_bootstrap_admin() {
  need_env
  # Trap 5: create-only, and it needs the management port to itself.
  local st; st="$("${DOCKER[@]}" inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo absent)"
  if [ "$st" = running ]; then
    say "stopping $CONTAINER (bootstrap-admin needs port ${MGMT_PORT} free)"
    "${DOCKER[@]}" stop "$CONTAINER" >/dev/null
  fi
  say "creating bootstrap admin (create-only; will not reset an existing user)"
  "${DOCKER[@]}" run --rm --network host --env-file "$RUNTIME_ENV" \
    "$IMAGE" bootstrap-admin user \
    --username "$(env_get KC_BOOTSTRAP_ADMIN_USERNAME "$RUNTIME_ENV")" \
    --password:env KC_BOOTSTRAP_ADMIN_PASSWORD 2>&1 \
    | grep -viE 'arjuna|stopping transaction|keycloak stopped' | tail -3 | sed 's/^/  /' || true
  cmd_start
}

case "${1:-}" in
  env)             cmd_env ;;
  start)           cmd_start ;;
  stop)            cmd_stop ;;
  restart)         cmd_restart ;;
  status)          cmd_status ;;
  import)          cmd_import ;;
  verify)          cmd_verify ;;
  export)          cmd_export ;;
  check-fixture)   cmd_check_fixture ;;
  bootstrap-admin) cmd_bootstrap_admin ;;
  *) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac

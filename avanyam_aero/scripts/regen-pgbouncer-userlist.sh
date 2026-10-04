#!/usr/bin/env bash
# Regenerate /etc/pgbouncer/userlist.txt from PostgreSQL's own SCRAM verifiers.
#
# Run as root on avanyam_aero after changing any pooled role's password:
#   sudo ./scripts/regen-pgbouncer-userlist.sh
#
# Why this script exists (K4):
#
# 1. NEVER write plaintext passwords here. PgBouncer needs to verify SCRAM on
#    the client side, and the only correct input is the SCRAM verifier already
#    stored in pg_authid. Reading it from there means a leaked userlist.txt is
#    not directly replayable as a password.
#
# 2. THE FIELDS MUST BE QUOTED. PgBouncer 1.22's userlist parser rejects the
#    unquoted `user verifier` form with a single terse line at startup:
#        pgbouncer[...]: broken auth file
#    ...and then keeps listening on the port anyway, so every connection fails
#    later with a misleading "SASL authentication failed". The cause is the
#    auth file, not the password. Quote both fields.
#
# 3. The verifier contains '$' characters, so it must never round-trip through
#    an unquoted shell expansion. Everything below happens inside a single
#    `psql -At` invocation using SQL string concatenation.

set -euo pipefail

ROLES="${1:-avanyam_app,keycloak,pgbouncer_admin}"

# Validate the role list before it reaches SQL: it is interpolated below, and
# this script runs as root.
if ! printf '%s' "$ROLES" | grep -Eq '^[A-Za-z0-9_,]+$'; then
    echo "refusing to run: role list must match [A-Za-z0-9_,]+" >&2
    exit 1
fi

sql="SELECT chr(34) || rolname || chr(34) || chr(32) || chr(34) || rolpassword || chr(34)
       FROM pg_authid
      WHERE rolname = ANY (string_to_array('${ROLES}', ','))
      ORDER BY rolname"

tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT

# Run as the postgres superuser: pg_authid is restricted, and pg_authid is
# readable only by superusers.
runuser -u postgres -- psql -v ON_ERROR_STOP=1 -At -c "$sql" > "$tmp"

if [ ! -s "$tmp" ]; then
    echo "refusing to install an EMPTY userlist (roles: ${ROLES})" >&2
    echo "check the role names exist in PostgreSQL first" >&2
    exit 1
fi

# Every line must have exactly two quoted fields.
if grep -qvE '^"[^"]+" "[^"]+"$' "$tmp"; then
    echo "refusing to install a malformed userlist (a line is not \"user\" \"verifier\")" >&2
    exit 1
fi

install -o postgres -g postgres -m 0640 "$tmp" /etc/pgbouncer/userlist.txt

echo "wrote /etc/pgbouncer/userlist.txt with $(wc -l < "$tmp") role(s):"
cut -d' ' -f1 "$tmp" | tr -d '"' | sed 's/^/  /'
echo "apply with: sudo systemctl reload pgbouncer"

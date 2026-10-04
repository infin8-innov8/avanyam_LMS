#!/usr/bin/env bash
# avanyam firewall policy (K7), per avanyam_intro.txt 13.10 DEFENSE IN DEPTH.
#
#   "Network policy: VM2 (data) and VM3 (media) accept traffic from VM1 only.
#    The app tier is the only internet-adjacent surface."
#
# and the one documented exception:
#
#   Keycloak on VM2 must reach the external corporate directory (OpenLDAP/AD)
#   on 636/LDAPS or 389+StartTLS, and that egress must be stated explicitly
#   rather than discovered during an outage.
#
# Usage: sudo ./apply-firewall.sh <tier>
#   tiers: dev | app | data | media
#
# IDEMPOTENT: safe to re-run. `ufw allow` is a no-op when the rule exists.

set -euo pipefail

TIER="${1:-}"
case "$TIER" in
    dev|app|data|media) ;;
    *) echo "usage: $0 <dev|app|data|media>" >&2; exit 2 ;;
esac

say() { printf '  %s\n' "$*"; }

echo "=== avanyam firewall: tier '$TIER' ==="

say "resetting to a known baseline"
ufw --force reset >/dev/null

say "defaults: deny incoming, allow outgoing"
ufw default deny incoming
ufw default allow outgoing

# ufw adds these itself, but state them so the policy is auditable in one place.
say "loopback always permitted"
ufw allow in on lo

case "$TIER" in
    dev)
        # All-in-one development laptop. Every service already binds 127.0.0.1
        # (verified with `ss -tlnp`), so the default-deny posture needs no
        # service exceptions at all.
        say "tier dev: all services are loopback-only; no inbound exceptions needed"
        say "  (postgres 5432, pgbouncer 6432, redis 6379, clamd 3310,"
        say "   seaweedfs s3 8333 -- all bound to 127.0.0.1)"
        ;;
    app)
        say "tier app (VM1): internet-adjacent, accepts 80/443 from anywhere"
        ufw allow 80/tcp  comment 'avanyam: HTTP -> nginx'
        ufw allow 443/tcp comment 'avanyam: HTTPS -> nginx'
        say "  SSH deliberately NOT opened. The spec does not require it and an"
        say "  exposed SSH port is a brute-force surface. Add it deliberately if"
        say "  you actually administer this host over the network."
        ;;
    data)
        say "tier data (VM2): accepts traffic from VM1 only"
        # Subnet, not a single host: the VM addresses are not pinned by the spec,
        # so the policy has to be expressed as a range. Tighten to /32 once the
        # VM1 address is fixed.
        ufw allow from 10.0.0.0/24 to any port 5432 comment 'avanyam: PG from app tier'
        ufw allow from 10.0.0.0/24 to any port 6432 comment 'avanyam: PgBouncer from app tier'
        say "tier data: Keycloak directory egress is OUTBOUND and must be scoped"
        say "  explicitly per 13.10. Add the real endpoint once known, e.g.:"
        say "    ufw allow out to <dir-ip> port 636 proto tcp comment 'avanyam: Keycloak -> corporate directory LDAPS'"
        say "  Do NOT leave this as 'allow any outbound' and call it documented."
        ;;
    media)
        say "tier media (VM3): accepts traffic from VM1 only"
        ufw allow from 10.0.0.0/24 to any port 8080 comment 'avanyam: SeaweedFS S3 from app tier'
        ufw allow from 10.0.0.0/24 to any port 9333 comment 'avanyam: SeaweedFS filer from app tier'
        ;;
esac

say "enabling ufw"
ufw --force enable

echo
echo "=== resulting policy ==="
ufw status verbose

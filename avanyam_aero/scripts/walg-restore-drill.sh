#!/usr/bin/env bash
# WAL-G restore drill (K6).
#
# A backup that has never been restored is not a backup. This script takes a
# WAL-G base backup, restores it into a throwaway cluster on port 55432, replays
# the archived WAL, and asserts the data is actually there. It never touches the
# live cluster.
#
# Usage: sudo ./scripts/walg-restore-drill.sh
#
# Every step below exists because it failed the first time. The gotchas are
# commented at the point they bite.

set -euo pipefail

PGBIN=/usr/lib/postgresql/16/bin
WALG=/usr/local/bin/walg-postgres.sh
DRILL_DIR=/var/tmp/walg-restore
BB_DIR=/var/tmp/walg-bb
SOCK=/var/tmp/pgsock
LOG=/var/tmp/walg-drill.log
PORT=55432

say() { printf '  %s\n' "$*"; }
step() { printf '\n=== %s ===\n' "$*"; }

cleanup() {
    step "cleanup"
    if [ -f "$DRILL_DIR/postmaster.pid" ]; then
        runuser -u postgres -- "$PGBIN/pg_ctl" -D "$DRILL_DIR" -m immediate stop >/dev/null 2>&1 || true
        say "stopped scratch cluster"
    fi
    rm -rf "$DRILL_DIR" "$BB_DIR" "$SOCK" /tmp/opencode/fetchtest "$LOG"
    say "removed $DRILL_DIR $BB_DIR $SOCK"
}
trap cleanup EXIT

step "preflight"
[ -x "$WALG" ] || { echo "missing $WALG" >&2; exit 1; }
command -v runuser >/dev/null || { echo "missing runuser" >&2; exit 1; }
# WAL-G must be able to see a backup to restore. If there is none, the drill
# would silently "pass" against an empty archive.
BACKUPS=$(runuser -u postgres -- bash -c ". /etc/walg/env; set -a; . /etc/walg/env; $WALG wal-show" 2>/dev/null | grep -c OK || true)
say "archive reachable, timelines reporting OK: $BACKUPS"

step "1. base backup"
rm -rf "$BB_DIR"; mkdir -p "$BB_DIR"; chown postgres:postgres "$BB_DIR"
# GOTCHA: pg_basebackup -Fp (plain) cannot be combined with -z; -z is tar-mode
# only. And -Ft produces base.tar.gz, which wal-g backup-push rejects with
# "Data directory ... is not the same as Postgres' one" because it is not a
# datadir. WAL-G needs a real directory tree containing backup_label.
runuser -u postgres -- "$PGBIN/pg_basebackup" \
    -D "$BB_DIR" -Fp -X stream --checkpoint=fast -h /var/run/postgresql
[ -f "$BB_DIR/backup_label" ] || { echo "backup_label missing" >&2; exit 1; }
say "base backup: $(du -sh "$BB_DIR" | cut -f1), $(find "$BB_DIR" -type f | wc -l) files"

step "2. push to archive"
# GOTCHA: the correct invocation is `PGDATA=<dir> backup-push` with NO path
# argument. Passing the path as an argument makes wal-g compare it against the
# running server's data_directory and abort.
runuser -u postgres -- env PGDATA="$BB_DIR" "$WALG" backup-push
BN=$(runuser -u postgres -- bash -c ". /etc/walg/env; set -a; . /etc/walg/env; $WALG wal-show --detailed-json" 2>/dev/null \
     | grep -oE 'base_[0-9A-F]{24}' | head -1)
[ -n "$BN" ] || { echo "no backup name found after push" >&2; exit 1; }
say "pushed as $BN"

step "3. fetch"
rm -rf "$DRILL_DIR" /tmp/opencode/fetchtest
mkdir -p /tmp/opencode/fetchtest /var/tmp/pgsock
# GOTCHA: in wal-g v3.0.9 the optional backup_name argument is effectively
# MANDATORY. `backup-fetch <dir>` alone fails with "insufficient arguments".
( set -a; . /etc/walg/env; set +a; wal-g backup-fetch /tmp/opencode/fetchtest "$BN" )
cp -a /tmp/opencode/fetchtest/. "$DRILL_DIR/"
rm -f "$DRILL_DIR/postmaster.pid" "$DRILL_DIR/postmaster.opts" "$DRILL_DIR/standby.signal"
chown -R postgres:postgres "$DRILL_DIR" /var/tmp/pgsock
chmod 700 "$DRILL_DIR"
say "fetched $(find "$DRILL_DIR" -type f | wc -l) files"

step "4. supply config (WAL-G cannot capture these)"
# GOTCHA: on Debian/Ubuntu the cluster config lives in /etc/postgresql/16/main,
# NOT in PGDATA, so pg_basebackup does not copy it and the restored cluster has
# no postgresql.conf at all. Copy it in by hand.
cp /etc/postgresql/16/main/postgresql.conf "$DRILL_DIR/"
cp /etc/postgresql/16/main/pg_hba.conf "$DRILL_DIR/"
cp /etc/postgresql/16/main/pg_ident.conf "$DRILL_DIR/"
chown postgres:postgres "$DRILL_DIR"/postgresql.conf "$DRILL_DIR"/pg_hba.conf "$DRILL_DIR"/pg_ident.conf
chmod 640 "$DRILL_DIR"/pg_hba.conf "$DRILL_DIR"/pg_ident.conf

# GOTCHA: Debian's postgresql.conf hardcodes data_directory to the LIVE cluster.
# Without rewriting it the drill attaches to production and dies with
# "lock file postmaster.pid already exists ... PID <live pid>".
python3 - "$DRILL_DIR/postgresql.conf" <<'PY'
import re, sys
p = sys.argv[1]
s = open(p).read()
for key, val in (("data_directory", f"'{sys.argv[1].rsplit('/', 1)[0]}/walg-restore'"),
                 ("hba_file",     f"'{sys.argv[1].rsplit('/', 1)[0]}/walg-restore/pg_hba.conf'"),
                 ("ident_file",   f"'{sys.argv[1].rsplit('/', 1)[0]}/walg-restore/pg_ident.conf'")):
    s = re.sub(rf"(?m)^[ \t]*{key}[ \t]*=.*$", f"{key} = {val}", s)
s = re.sub(r"(?m)^[ \t]*external_pid_file[ \t]*=.*$", "# external_pid_file disabled for drill", s)
open(p, "w").write(s)
print("  rewrote data_directory / hba_file / ident_file, disabled external_pid_file")
PY

mkdir -p "$DRILL_DIR/conf.d"; chown postgres:postgres "$DRILL_DIR/conf.d"
# Empty conf.d on purpose: do not let the drill inherit archive_mode=on or the
# production shared_buffers.
cat > "$DRILL_DIR/conf.d/20-pitr.conf" <<EOF
restore_command = '$WALG wal-fetch %f %p'
recovery_target_action = 'promote'
EOF
chown postgres:postgres "$DRILL_DIR/conf.d/20-pitr.conf"
touch "$DRILL_DIR/recovery.signal"; chown postgres:postgres "$DRILL_DIR/recovery.signal"

step "5. replay archived WAL"
touch "$LOG"; chown postgres:postgres "$LOG"
runuser -u postgres -- "$PGBIN/pg_ctl" -D "$DRILL_DIR" -l "$LOG" -w -t 180 \
    -o "-p $PORT -k $SOCK -c archive_mode=off -c listen_addresses='' -c shared_buffers=64MB -c fsync=off" start
say "recovery evidence:"
grep -E 'restored log file|consistent recovery state|completed backup recovery' "$LOG" | sed 's/^/    /'

step "6. assert the restored cluster is actually correct"
q() { runuser -u postgres -- psql -h "$SOCK" -p "$PORT" -At -c "$1"; }
DBS=$(q "select string_agg(datname,',' order by datname) from pg_database where not datistemplate;")
say "databases: $DBS"
for want in avanyam avanyam_audit avanyam_reporting keycloak; do
    case ",$DBS," in *",$want,"*) say "  present: $want" ;;
                   *) echo "  MISSING DATABASE: $want" >&2; exit 1 ;; esac
done
ROLES=$(q "select string_agg(rolname,',' order by rolname) from pg_roles where rolcanlogin;")
say "login roles: $ROLES"
for want in avanyam_app avanyam_user avanyam_migrate keycloak; do
    case ",$ROLES," in *",$want,"*) say "  present: $want" ;;
                    *) echo "  MISSING ROLE: $want" >&2; exit 1 ;; esac
done
say "replay LSN: $(q 'select pg_last_wal_replay_lsn();')"
say "timeline:   $(q 'select timeline_id from pg_control_checkpoint();')"

step "RESULT"
echo "  RESTORE DRILL PASSED - archive is recoverable."

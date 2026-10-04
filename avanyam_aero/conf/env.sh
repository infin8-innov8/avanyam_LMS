# avanyam_aero runtime environment — sourced before any service command.
export AERO_ROOT="/home/rupesh/avanyam_lms/avanyam_aero"
export AERO_OPT="$AERO_ROOT/opt"
export AERO_CONF="$AERO_ROOT/conf"
export AERO_DATA="$AERO_ROOT/data"
export AERO_LOGS="$AERO_ROOT/logs"
export AERO_RUN="$AERO_ROOT/run"

# PostgreSQL lives in the Debian-packaged system layout, NOT under $AERO_ROOT.
# The old values ($AERO_DATA/pgdata, $AERO_OPT/bin) pointed at directories that
# have never existed on this host, so PGDATA was silently wrong for every
# service that sourced this file. Verified against `psql -c 'show data_directory'`.
export PGDATA="${PGDATA:-/var/lib/postgresql/16/main}"
export PGPORT="${PGPORT:-5432}"
export PGHOST="${PGHOST:-/var/run/postgresql}"
export PGBIN="${PGBIN:-/usr/lib/postgresql/16/bin}"

# Put the server binaries (pg_ctl, pg_basebackup, pg_receivewal, pg_controldata)
# ahead of the client wrappers in /usr/bin so `-p <pgdata>` style invocations work.
export PATH="$PGBIN:$PATH"

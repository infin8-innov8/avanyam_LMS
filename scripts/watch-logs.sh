#!/usr/bin/env bash
#
# watch-logs.sh -- every log this stack writes, live, in one terminal.
#
# Built for the testing loop: start this in one pane, exercise the app in
# another, and see the request, the query it ran, the Celery task it dispatched
# and the mail it sent without switching windows.
#
# Why a script and not a `tail -F a b c`: `tail` with many files prints its own
# `==> file <==` banner every time it reopens one, and it cannot follow a
# journal at all. Each source here is tailed separately and prefixed, so a line
# always says where it came from.
#
# Usage:
#   scripts/watch-logs.sh              # everything
#   scripts/watch-logs.sh signup       # only lines matching /signup/i
#   HISTORY=20 scripts/watch-logs.sh   # show 20 lines of backlog before following
#
# Ctrl-C stops everything, including the tails.

set -uo pipefail

FILTER="${1:-}"
HISTORY="${HISTORY:-0}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Only colour when a human is watching. Piping to grep or a file should not
# embed escape codes in the output.
if [ -t 1 ]; then
  C_RESET=$'\033[0m'; C_DIM=$'\033[2m'
  C_APP=$'\033[36m';    C_WORKER=$'\033[35m'; C_DB=$'\033[33m'
  C_WEB=$'\033[34m';   C_CACHE=$'\033[32m'
else
  C_RESET=''; C_DIM=''; C_APP=''; C_WORKER=''; C_DB=''; C_WEB=''; C_CACHE=''
fi

pids=()

cleanup() {
  # Kill the tails, not just this script. Without the trap, Ctrl-C leaves a
  # dozen `tail -F` processes behind holding the files open.
  trap - INT TERM EXIT
  [ ${#pids[@]} -gt 0 ] && kill "${pids[@]}" 2>/dev/null
  wait 2>/dev/null
}
trap cleanup INT TERM EXIT

# follow <tag> <colour> <command...>
#
# Runs a producer, stamps every line with its source, applies the optional
# filter, and records the pid so cleanup can reach it. `sed -u` matters: without
# it sed buffers a block at a time and the log appears to stall for a second
# before dumping, which reads as "nothing is happening".
follow() {
  local tag="$1" colour="$2"; shift 2
  local label
  label="$(printf '%s' "${colour}[${tag}]${C_RESET}")"

  if [ -n "$FILTER" ]; then
    "$@" 2>&1 | sed -u "s|^|${label} |" | grep -i -- "$FILTER" &
  else
    "$@" 2>&1 | sed -u "s|^|${label} |" &
  fi
  pids+=("$!")
}

# follow_file <tag> <colour> <path>
#
# Skips a source that is missing or unreadable and says so once, instead of
# letting `tail` print an error every time it reopens the file. Postgres in
# particular is only readable because this user is in `adm`.
follow_file() {
  local tag="$1" colour="$2" path="$3"
  if [ ! -e "$path" ]; then
    printf '%s[skip]%s %s -- not present on this host\n' "$C_DIM" "$C_RESET" "$path"
    return
  fi
  if [ ! -r "$path" ]; then
    printf '%s[skip]%s %s -- not readable (try: sudo groups, or add yourself to adm)\n' \
      "$C_DIM" "$C_RESET" "$path"
    return
  fi
  follow "$tag" "$colour" tail -n "$HISTORY" -F "$path"
}

echo "$(printf '%s' "${C_DIM}")watching: press Ctrl-C to stop${C_RESET}"
echo "$(printf '%s' "${C_DIM}")sources below; only readable ones are attached${C_RESET}"
echo

# The application. runserver's access lines land here, and because DEBUG is on
# in dev the SQL for each request is interleaved -- which is the point when you
# are trying to see why a page was slow or why a write did not land.
follow_file app "$C_APP" "/tmp/opencode/server.log"

# The Celery worker. Runs under a *user* systemd unit, so this is
# `journalctl --user`, not the system journal -- a very easy thing to get wrong
# and then conclude the worker is silent.
if systemctl --user is-active --quiet avanyam-lms-celery; then
  follow worker "$C_WORKER" journalctl --user -u avanyam-lms-celery -f -o cat -n "$HISTORY"
else
  printf '%s[skip]%s avanyam-lms-celery -- unit is %s, not running\n' \
    "$C_DIM" "$C_RESET" "$(systemctl --user is-active avanyam-lms-celery 2>/dev/null || echo unknown)"
fi

# Redis has no logfile configured, so it goes to the system journal.
follow cache "$C_CACHE" journalctl -u redis-server -f -o cat -n "$HISTORY"

# The database and its pooler. Statement logging is off by default, so this is
# connection-level noise plus anything pgbouncer complains about; it is still
# where "too many connections" and "server closed the connection unexpectedly"
# show up, which are the two failures that look like random 500s from the app.
follow_file db "$C_DB" /var/log/postgresql/postgresql-16-main.log
follow_file pool "$C_DB" /var/log/postgresql/pgbouncer.log

# nginx sits in front for the non-dev paths.
follow_file web "$C_WEB" /var/log/nginx/error.log

wait

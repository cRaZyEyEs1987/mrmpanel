#!/bin/sh
# Ensure SQLite schema exists and is owned by pdns, then hand off to official startup.
set -eu
DBDIR=/var/lib/powerdns
DB="${PDNS_DB:-$DBDIR/pdns.sqlite3}"
SCHEMA="${PDNS_SCHEMA:-/etc/powerdns/schema.sqlite3.sql}"

mkdir -p "$DBDIR"
# Host bind mounts are often root-owned; pdns (uid 953) must write the DB.
chown pdns:pdns "$DBDIR" 2>/dev/null || true
chmod 775 "$DBDIR" 2>/dev/null || true

if [ ! -s "$DB" ]; then
  echo "[mrmpanel-pdns] Initializing SQLite schema at $DB"
  # Prefer packaged empty DB from the image if present under a backup path
  if [ -f /usr/share/pdns/pdns.sqlite3 ]; then
    cp /usr/share/pdns/pdns.sqlite3 "$DB"
  else
    sqlite3 "$DB" < "$SCHEMA"
  fi
  chown pdns:pdns "$DB"
  chmod 640 "$DB"
fi

exec /usr/bin/tini -- /usr/local/sbin/pdns_server-startup "$@"

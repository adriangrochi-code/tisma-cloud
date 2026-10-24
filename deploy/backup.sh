#!/bin/sh
# Backups of the database of the master (accounts, payments, plans, jobs). Runs in the "backup" service of
# compose.master.yaml, or by hand. Every day at BACKUP_HOUR (UTC) a pg_dump in custom format goes to BACKUP_DIR, is
# checked (pg_restore --list must read it) and the old ones are removed: the last BACKUP_DAYS daily copies, and one per
# week (Sunday) for BACKUP_WEEKS weeks.
#
#   backup.sh          run forever (the service)
#   backup.sh now      one backup now, then exit
#
# Connection: the PG* variables of libpq (PGHOST, PGUSER, PGPASSWORD, PGDATABASE).
# Job files (projects and results) are not backed up: they are deleted after a few days anyway.
set -eu

BACKUP_DIR=${BACKUP_DIR:-/backups}
BACKUP_HOUR=${BACKUP_HOUR:-3}
BACKUP_DAYS=${BACKUP_DAYS:-14}
BACKUP_WEEKS=${BACKUP_WEEKS:-8}

log() { echo "$(date -u '+%Y-%m-%d %H:%M:%S') backup: $*"; }

backup_once() {
    mkdir -p "$BACKUP_DIR/daily" "$BACKUP_DIR/weekly"
    stamp=$(date -u +%Y%m%d-%H%M)
    file="$BACKUP_DIR/daily/tisma-$stamp.dump"
    # Written aside and renamed when complete and readable: a cut backup never looks like a good one.
    if ! pg_dump --format=custom --compress=6 --file="$file.part" || ! pg_restore --list "$file.part" > /dev/null; then
        rm -f "$file.part"
        return 1
    fi
    mv "$file.part" "$file"
    log "wrote $file ($(du -h "$file" | cut -f1))"
    if [ "$(date -u +%u)" = 7 ]; then
        cp "$file" "$BACKUP_DIR/weekly/"
        log "weekly copy kept"
    fi
    prune "$BACKUP_DIR/daily" "$BACKUP_DAYS"
    prune "$BACKUP_DIR/weekly" "$BACKUP_WEEKS"
}

# Keeps the newest $2 backups of directory $1 (the names sort by date).
prune() {
    ls -1 "$1"/tisma-*.dump 2> /dev/null | sort -r | tail -n +"$(($2 + 1))" | while read -r old; do
        rm -f "$old"
        log "removed $old"
    done
    rm -f "$1"/*.part
}

seconds_until_next_run() {
    now=$(date -u +%s)
    today=$(date -u +%Y-%m-%d)
    next=$(date -u -d "$today $BACKUP_HOUR:00:00" +%s)
    [ "$next" -gt "$now" ] || next=$((next + 86400))
    echo $((next - now))
}

if [ "${1:-}" = now ]; then
    backup_once || { log "FAILED"; exit 1; }
    exit 0
fi

log "daily backups at $BACKUP_HOUR:00 UTC into $BACKUP_DIR (keep $BACKUP_DAYS days, $BACKUP_WEEKS weeks)"
while true; do
    sleep "$(seconds_until_next_run)"
    # A failed backup is logged and retried the next day; the service keeps running.
    backup_once || log "FAILED (see the messages above)"
done

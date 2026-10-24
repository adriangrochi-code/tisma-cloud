#!/bin/sh
# Restores a backup of backup.sh into the database of the master, replacing its contents. Stop the API first, so that
# nothing writes during the restore:
#
#   docker compose --env-file .env -f deploy/compose.master.yaml stop api
#   docker compose --env-file .env -f deploy/compose.master.yaml run --rm backup restore.sh /backups/daily/tisma-....dump
#   docker compose --env-file .env -f deploy/compose.master.yaml start api
#
# Connection: the PG* variables of libpq, as backup.sh.
set -eu

file=${1:?usage: restore.sh <backup file>}
pg_restore --list "$file" > /dev/null
echo "restoring $file into database ${PGDATABASE:-?} on ${PGHOST:-?}"
# --clean --if-exists: drop what the backup has and create it again, in one transaction (all or nothing).
pg_restore --clean --if-exists --no-owner --single-transaction --dbname="${PGDATABASE}" "$file"
echo "restored"

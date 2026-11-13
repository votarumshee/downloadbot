#!/bin/sh
# Database backups only. Keep the encryption key in a separate recovery location.
set -eu
umask 077
cd /srv/downloadbot
backup_root=/var/backups/downloadbot
install -d -m 0700 "$backup_root"
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
temporary="$backup_root/$timestamp.dump.part"
destination="$backup_root/$timestamp.dump"
trap 'rm -f -- "$temporary"' EXIT HUP INT TERM
docker compose --env-file /etc/downloadbot/deployment.env \
  -f compose.yaml -f deploy/compose.vds.yaml \
  exec -T db pg_dump -U downloadbot -d downloadbot -Fc > "$temporary"
test -s "$temporary"
mv -- "$temporary" "$destination"
# Verify the archive is readable before removing older backups.
docker compose --env-file /etc/downloadbot/deployment.env \
  -f compose.yaml -f deploy/compose.vds.yaml \
  exec -T db pg_restore --list < "$destination" > /dev/null
find "$backup_root" -maxdepth 1 -type f -name '*.dump' -mtime +7 -delete
printf 'Created PostgreSQL backup: %s\n' "$destination"

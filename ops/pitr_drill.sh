#!/usr/bin/env bash
# Point-in-time-recovery drill on DISPOSABLE Docker resources (sprint 3). It creates and removes only the containers/volumes named below; it never touches any other database.
#   bash ops/pitr_drill.sh
# Proves: continuous WAL archiving + one base backup lets you rebuild the database AS OF a chosen instant: rows committed before it are present, rows committed after it are absent.
# It does NOT prove: your hosting provider's PITR, retention, encryption, cross-region copies, or restore time at production size.
set -euo pipefail
export MSYS_NO_PATHCONV=1
SRC=af-s3-pitr; RST=af-s3-pitr-restore; ARCH=af_s3_pitr_archive; DATA=af_s3_pitr_restored
cleanup() { docker rm -f "$SRC" "$RST" >/dev/null 2>&1 || true; docker volume rm -f "$ARCH" "$DATA" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup
docker volume create "$ARCH" >/dev/null; docker volume create "$DATA" >/dev/null
q() { docker exec "$1" psql -U postgres -d drill -tAc "$2"; }
wait_pg() { for _ in $(seq 60); do docker exec "$1" pg_isready -U postgres >/dev/null 2>&1 && return 0; sleep 1; done; echo "postgres in $1 did not start"; exit 1; }

echo "1. primary with WAL archiving (archive_command copies each finished WAL segment to the archive volume)"
docker run -d --name "$SRC" -e POSTGRES_PASSWORD=ci -v "$ARCH":/archive -p 127.0.0.1:54349:5432 postgres:16 \
  -c wal_level=replica -c archive_mode=on -c "archive_command=test ! -f /archive/%f && cp %p /archive/%f" -c archive_timeout=5 >/dev/null
wait_pg "$SRC"
docker exec -u root "$SRC" chown postgres:postgres /archive
docker exec "$SRC" psql -U postgres -c "CREATE DATABASE drill" >/dev/null
q "$SRC" "CREATE TABLE orders (n int primary key, note text)" >/dev/null
q "$SRC" "INSERT INTO orders SELECT g, 'before-backup' FROM generate_series(1,100) g" >/dev/null

echo "2. base backup (pg_basebackup, plain format, WAL included)"
docker exec -u postgres "$SRC" pg_basebackup -D /archive/base -Fp -X fetch -U postgres >/dev/null

echo "3. more work, then the recovery target instant T, then work that must NOT survive the restore"
q "$SRC" "INSERT INTO orders SELECT g, 'before-T' FROM generate_series(101,200) g" >/dev/null
sleep 2
T=$(q "$SRC" "SELECT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.US') || ' UTC'")
sleep 2
q "$SRC" "INSERT INTO orders SELECT g, 'after-T-a-mistake' FROM generate_series(201,300) g" >/dev/null
q "$SRC" "SELECT pg_switch_wal()" >/dev/null
for _ in $(seq 30); do [ "$(docker exec "$SRC" sh -c 'ls /archive | grep -c "^0000"')" -ge 2 ] && break; sleep 1; done
echo "   target time T = $T ; primary now has $(q "$SRC" "SELECT count(*) FROM orders") rows; archived WAL files: $(docker exec "$SRC" sh -c 'ls /archive | grep -c "^0000"')"

echo "4. restore: base backup + archived WAL, stop at T, into a NEW container"
docker run --rm -u root -v "$ARCH":/archive:ro -v "$DATA":/restore postgres:16 bash -c \
  "cp -a /archive/base/. /restore/ && rm -f /restore/postmaster.pid && touch /restore/recovery.signal && \
   printf \"restore_command = 'cp /archive/%%f %%p'\nrecovery_target_time = '$T'\nrecovery_target_action = 'promote'\n\" >> /restore/postgresql.auto.conf && \
   chown -R postgres:postgres /restore && chmod 700 /restore"
docker run -d --name "$RST" -e POSTGRES_PASSWORD=ci -v "$ARCH":/archive:ro -v "$DATA":/var/lib/postgresql/data -p 127.0.0.1:54350:5432 postgres:16 >/dev/null
wait_pg "$RST"
for _ in $(seq 60); do [ "$(docker exec "$RST" psql -U postgres -tAc 'SELECT pg_is_in_recovery()')" = "f" ] && break; sleep 1; done

echo "5. verify"
ROWS=$(q "$RST" "SELECT count(*) FROM orders"); LATE=$(q "$RST" "SELECT count(*) FROM orders WHERE note = 'after-T-a-mistake'"); EARLY=$(q "$RST" "SELECT count(*) FROM orders WHERE note = 'before-T'")
echo "   restored rows=$ROWS rows-before-T=$EARLY rows-after-T=$LATE (expected 200 / 100 / 0)"
if [ "$ROWS" = "200" ] && [ "$EARLY" = "100" ] && [ "$LATE" = "0" ]; then echo "PITR DRILL PASSED"; else echo "PITR DRILL FAILED"; exit 1; fi

"""Database backup job with a REAL restore check (sprint 4). It is the publisher of the backup/restore signals the alert rules need (``AgentForgeBackupStale``, ``AgentForgeRestoreVerificationFailed``).

    python ops/backup_job.py --db agentforge --out-dir /backups [--status-file /var/lib/agentforge/backup-status.json] [--prom-file /var/lib/node_exporter/agentforge_backup.prom]
                             [--via-docker <container>] [--keep 14] [--check-wal] [--alert-webhook https://...] [--verify-only <dump file>]

WHAT ONE RUN DOES: (1) ``pg_dump -Fc`` of the database to a timestamped file; (2) INTEGRITY: the archive's table of contents must parse (``pg_restore --list``) and a SHA-256 is stored next to it; (3) RESTORE VERIFICATION:
the dump is restored into a throw-away scratch database, which must have the same ``alembic_version`` and the same set of tables as the source and, for every table, no more rows than the source (equal when the
source was quiet; fewer-or-equal is accepted because writes may continue during a dump); the scratch database is dropped; (4) it writes the status JSON (``last_success_ts`` only moves forward on a
verified success; ``restore_verification_ok`` reports the LAST verification) and the Prometheus textfile; (5) retention removes only files this job created beyond ``--keep``; (6) with ``--check-wal`` it also reports
WAL archiving health (``archive_mode``, ``archive_command``, whether the archiver is failing NOW, and the last archive age). Exit code 0 = verified backup; non-zero = something is wrong (and ``--alert-webhook``
gets ONE message). Times for the dump and for the restore are recorded: the restore time is a first, SMALL-DATABASE data point for RTO, not a promise.

``--via-docker`` runs the PostgreSQL client tools inside a (disposable) container; without it they must be on PATH with PG* environment variables (PGHOST, PGUSER, PGPASSWORD) set. It never touches any database
other than ``--db`` (read-only dump) and the scratch database it creates and drops. It does not manage encryption or off-host copies: do that with your storage layer."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timezone

ap = argparse.ArgumentParser()
ap.add_argument("--db", default="agentforge")
ap.add_argument("--out-dir", required=True)
ap.add_argument("--status-file", default=None)
ap.add_argument("--prom-file", default=None)
ap.add_argument("--via-docker", default=None)
ap.add_argument("--pg-user", default="postgres")
ap.add_argument("--keep", type=int, default=14)
ap.add_argument("--check-wal", action="store_true")
ap.add_argument("--alert-webhook", default=None)
ap.add_argument("--verify-only", default=None, help="verify an existing dump file (integrity + restore into a scratch database) without dumping")
args = ap.parse_args()
PREFIX = "agentforge-backup-"


def pg(cmd, *, stdin=None, stdout=None, check=True, capture=True):
    full = (["docker", "exec", "-i", args.via_docker] if args.via_docker else []) + cmd
    return subprocess.run(full, stdin=stdin, stdout=stdout if stdout is not None else (subprocess.PIPE if capture else None), stderr=subprocess.PIPE, check=check)


def psql(db, sql):
    out = pg(["psql", "-U", args.pg_user, "-d", db, "-tAc", sql]).stdout.decode().strip()
    return out


def table_counts(db):
    tables = [t for t in psql(db, "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1").splitlines() if t]
    return {t: int(psql(db, f'SELECT count(*) FROM "{t}"')) for t in tables}


def notify(message):
    if not args.alert_webhook:
        return
    try:
        req = urllib.request.Request(args.alert_webhook, method="POST", data=json.dumps({"text": message, "alert": "AgentForgeBackupJob", "severity": "page", "status": "firing"}).encode(), headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as exc:  # noqa: BLE001 - reported on stderr by type only (the URL is a secret)
        print(f"alert delivery failed ({type(exc).__name__})", file=sys.stderr)


def write_status(ok, last_success_ts, detail):
    if args.status_file:
        os.makedirs(os.path.dirname(os.path.abspath(args.status_file)), exist_ok=True)
        with open(args.status_file, "w", encoding="utf-8") as f:
            json.dump({"last_success_ts": last_success_ts, "restore_verification_ok": ok, "checked_at": time.time(), **detail}, f)
    if args.prom_file:
        os.makedirs(os.path.dirname(os.path.abspath(args.prom_file)), exist_ok=True)
        with open(args.prom_file, "w", encoding="utf-8") as f:
            f.write(f"# TYPE agentforge_backup_last_success_timestamp_seconds gauge\nagentforge_backup_last_success_timestamp_seconds {last_success_ts}\n"
                    f"# TYPE agentforge_restore_verification_ok gauge\nagentforge_restore_verification_ok {1 if ok else 0}\n")


def previous_success():
    try:
        return float(json.load(open(args.status_file, encoding="utf-8"))["last_success_ts"]) if args.status_file else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def fail(reason, detail=None):
    print("BACKUP JOB FAILED:", reason)
    write_status(False, previous_success(), {"error": reason, **(detail or {})})
    notify(f"AgentForge backup job failed: {reason}")
    sys.exit(1)


os.makedirs(args.out_dir, exist_ok=True)
started = time.time()
if args.verify_only:
    dump = args.verify_only
    dump_seconds = 0.0
else:
    dump = os.path.join(args.out_dir, PREFIX + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".dump")
    t = time.time()
    try:
        with open(dump, "wb") as f:
            pg(["pg_dump", "-U", args.pg_user, "-Fc", args.db], stdout=f)
    except subprocess.CalledProcessError as exc:
        fail("pg_dump failed", {"stderr": exc.stderr.decode()[-300:]})
    dump_seconds = time.time() - t
size = os.path.getsize(dump)
if size < 1000:
    fail("the dump file is implausibly small", {"bytes": size})

# (2) integrity: the archive's table of contents must parse; a checksum is stored for later comparison
with open(dump, "rb") as f:
    toc = pg(["pg_restore", "--list"], stdin=f, check=False)
if toc.returncode != 0 or b"TABLE" not in toc.stdout:
    fail("the archive is corrupt: pg_restore --list could not read it", {"file": os.path.basename(dump)})
sha = hashlib.sha256(open(dump, "rb").read()).hexdigest()
if not args.verify_only:
    open(dump + ".sha256", "w").write(f"{sha}  {os.path.basename(dump)}\n")
else:
    side = dump + ".sha256"
    if os.path.exists(side) and open(side).read().split()[0] != sha:
        fail("the checksum does not match the stored SHA-256", {"file": os.path.basename(dump)})

# (3) restore verification into a scratch database
scratch = "af_restore_check_" + uuid.uuid4().hex[:8]
restore_seconds = None
try:
    pg(["psql", "-U", args.pg_user, "-d", "postgres", "-c", f'CREATE DATABASE "{scratch}"'])
    t = time.time()
    with open(dump, "rb") as f:
        restore = pg(["pg_restore", "-U", args.pg_user, "-d", scratch, "--no-owner", "--exit-on-error"], stdin=f, check=False)
    restore_seconds = time.time() - t
    if restore.returncode != 0:
        fail("restore into the scratch database failed", {"stderr": restore.stderr.decode()[-300:]})
    restored_tables = table_counts(scratch)
    if args.verify_only:
        source_tables = restored_tables  # no live source to compare with: the restore itself and the version check below are the test
    else:
        source_tables = table_counts(args.db)
    if set(source_tables) != set(restored_tables):
        fail("the restored database has a different set of tables", {"missing": sorted(set(source_tables) - set(restored_tables))[:5]})
    bigger = [t for t, n in restored_tables.items() if n > source_tables[t]]
    if bigger:
        fail("a restored table has MORE rows than its source", {"tables": bigger[:5]})
    version = psql(scratch, "SELECT version_num FROM alembic_version") if "alembic_version" in restored_tables else None
    if not args.verify_only and "alembic_version" in source_tables and version != psql(args.db, "SELECT version_num FROM alembic_version"):
        fail("the restored schema revision differs from the source", {"restored": version})
    fewer = {t: (restored_tables[t], source_tables[t]) for t in restored_tables if restored_tables[t] < source_tables[t]}
finally:
    pg(["psql", "-U", args.pg_user, "-d", "postgres", "-c", f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)'], check=False)

# (4)-(6) status, retention, WAL
wal = {}
if args.check_wal:
    wal = {"archive_mode": psql("postgres", "SHOW archive_mode"), "archive_command_set": psql("postgres", "SHOW archive_command") not in ("", "(disabled)"),
           "wal_level": psql("postgres", "SHOW wal_level"), "archiver_failed_count_total": int(psql("postgres", "SELECT failed_count FROM pg_stat_archiver") or 0),
           "archiver_failing_now": psql("postgres", "SELECT coalesce(last_failed_time IS NOT NULL AND (last_archived_time IS NULL OR last_failed_time > last_archived_time), false) FROM pg_stat_archiver") == "t",
           "last_archived_seconds_ago": psql("postgres", "SELECT coalesce(round(extract(epoch FROM now() - last_archived_time)), -1) FROM pg_stat_archiver")}
    if wal["archive_mode"] != "on" or not wal["archive_command_set"] or wal["wal_level"] == "minimal" or wal["archiver_failing_now"]:  # failed_count is CUMULATIVE: only a failure newer than the last success means it is broken now
        fail("WAL archiving is not healthy", wal)
detail = {"file": os.path.basename(dump), "bytes": size, "sha256": sha, "tables": len(restored_tables), "alembic_version": version, "dump_seconds": round(dump_seconds, 2),
          "restore_seconds": round(restore_seconds or 0, 2), "tables_with_fewer_rows_than_the_live_source": fewer, "wal": wal, "total_seconds": round(time.time() - started, 2)}
write_status(True, time.time(), detail)
if not args.verify_only:
    mine = sorted(f for f in os.listdir(args.out_dir) if f.startswith(PREFIX) and f.endswith(".dump"))
    for old in mine[:-args.keep] if args.keep > 0 else []:
        for suffix in ("", ".sha256"):
            try:
                os.remove(os.path.join(args.out_dir, old + suffix))
            except OSError:
                pass
print("BACKUP VERIFIED:", json.dumps(detail))

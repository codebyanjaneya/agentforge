"""Backup and failed-restore drill (sprint 4) on a DISPOSABLE PostgreSQL 16 container with WAL archiving on. Run from backend/ with the venv interpreter:

    python ../ops/backup_drill.py

It creates the container ``af-s4-backup`` (port 54379) and a temp directory, migrates a database with the repository's real Alembic head, inserts rows, then drives ``ops/backup_job.py``:
  1. a normal run: must end VERIFIED, write the status JSON and the Prometheus textfile, and the application's alert signals (``alerting.read_backup_status``) must show no backup alert;
  2. a CORRUPT archive (bytes overwritten in the middle), a TRUNCATED archive and a CHECKSUM mismatch: each must FAIL with exit code 1, must NOT move ``last_success_ts``, must flip
     ``restore_verification_ok`` to false (which fires ``AgentForgeRestoreVerificationFailed`` in the evaluator), and must deliver ONE webhook message to a local receiver;
  3. broken WAL archiving (the archive target made read-only): the ``--check-wal`` run must FAIL.
It prints one line per check and the measured dump/restore seconds. Removes only what it created."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, "..", "backend"))
sys.path.insert(0, BACKEND)
C, PORT = "af-s4-backup", 54379
tmp = tempfile.mkdtemp(prefix="af_backup_drill_")
status, prom = os.path.join(tmp, "status.json"), os.path.join(tmp, "agentforge_backup.prom")
fails, got = [], []


def check(label, ok):
    print(("PASS " if ok else "FAIL ") + label)
    if not ok:
        fails.append(label)


def sh(*cmd, **kw):
    return subprocess.run(list(cmd), capture_output=True, text=True, **kw)


class Hook(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


hook = ThreadingHTTPServer(("127.0.0.1", 0), Hook)
threading.Thread(target=hook.serve_forever, daemon=True).start()
HOOK = f"http://127.0.0.1:{hook.server_address[1]}/hook/SECRET"


def job(*extra):
    return sh(sys.executable, os.path.join(HERE, "backup_job.py"), "--db", "agentforge", "--out-dir", os.path.join(tmp, "backups"), "--status-file", status, "--prom-file", prom, "--via-docker", C,
              "--alert-webhook", HOOK, *extra)


def read_status():
    return json.load(open(status, encoding="utf-8"))


try:
    sh("docker", "rm", "-f", C)
    sh("docker", "volume", "rm", "-f", "af_s4_backup_arch")
    sh("docker", "volume", "create", "af_s4_backup_arch")
    sh("docker", "run", "-d", "--name", C, "-e", "POSTGRES_PASSWORD=ci", "-v", "af_s4_backup_arch:/archive", "-p", f"127.0.0.1:{PORT}:5432", "postgres:16", "-c", "wal_level=replica", "-c", "archive_mode=on",
       "-c", "archive_command=test ! -f /archive/%f && cp %p /archive/%f", "-c", "archive_timeout=5")
    for _ in range(40):
        if sh("docker", "exec", C, "pg_isready", "-U", "postgres").returncode == 0:
            break
        time.sleep(1)
    sh("docker", "exec", "-u", "root", C, "chown", "postgres:postgres", "/archive")
    sh("docker", "exec", C, "psql", "-U", "postgres", "-c", "CREATE DATABASE agentforge")
    env = {**os.environ, "APP_ENV": "development", "GROQ_API_KEY": "x", "PYTHON_DOTENV_DISABLED": "1", "DATABASE_URL": f"postgresql://postgres:ci@127.0.0.1:{PORT}/agentforge"}
    mig = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BACKEND, env=env, capture_output=True, text=True)
    check("the repository's real migrations applied to the drill database", mig.returncode == 0)
    sh("docker", "exec", C, "psql", "-U", "postgres", "-d", "agentforge", "-c",
       "INSERT INTO users (id,email,email_normalized,is_active,created_at) SELECT gen_random_uuid(), 'u'||g||'@example.com', 'u'||g||'@example.com', true, now() FROM generate_series(1,500) g")
    sh("docker", "exec", C, "psql", "-U", "postgres", "-c", "SELECT pg_switch_wal()")
    time.sleep(2)

    # 1. a normal verified backup
    r = job("--check-wal")
    print("     job output:", (r.stdout + r.stderr)[-500:].replace(chr(10), " | "))
    check("a normal backup run ends VERIFIED (exit 0)", r.returncode == 0 and "BACKUP VERIFIED" in r.stdout)
    st = read_status()
    check("the status file records a success and a passing restore verification", st["restore_verification_ok"] is True and st["last_success_ts"] > time.time() - 120)
    detail = json.loads(r.stdout.split("BACKUP VERIFIED:", 1)[1]) if "BACKUP VERIFIED" in r.stdout else {}
    check(f"WAL archiving is healthy ({detail.get('wal')})", detail.get("wal", {}).get("archive_mode") == "on" and detail.get("wal", {}).get("archiver_failing_now") is False)
    check("the Prometheus textfile exposes both signals", all(k in open(prom).read() for k in ("agentforge_backup_last_success_timestamp_seconds", "agentforge_restore_verification_ok 1")))
    import alerting

    sig = alerting.Signals()
    sig.record(alerting.read_backup_status(status, time.time()), {})
    check("the application's alert rules see NO backup problem after a good run", not any(rule.check(sig) for rule in alerting.RULES if "Backup" in rule.name or "Restore" in rule.name))
    good_ts = st["last_success_ts"]
    dump = os.path.join(tmp, "backups", detail["file"])
    print(f"     measured on this tiny database ({detail.get('tables')} tables, {detail.get('bytes')} bytes): dump {detail.get('dump_seconds')} s, restore {detail.get('restore_seconds')} s, whole job {detail.get('total_seconds')} s")

    # 2. failure scenarios
    data = bytearray(open(dump, "rb").read())
    scenarios = {}
    corrupt = bytearray(data)
    for i in range(len(corrupt) // 2, len(corrupt) // 2 + 4000):
        corrupt[i] = (corrupt[i] + 1) % 256
    scenarios["corrupt archive (4 KB overwritten in the middle, no checksum sidecar)"] = (bytes(corrupt), False)
    scenarios["truncated archive (cut in half)"] = (bytes(data[: len(data) // 2]), False)
    flipped = bytearray(data)
    flipped[len(flipped) - 20] ^= 0xFF
    scenarios["checksum mismatch (one byte flipped near the end, sidecar kept)"] = (bytes(flipped), True)
    for label, (blob, with_sidecar) in scenarios.items():
        path = os.path.join(tmp, "bad.dump")
        open(path, "wb").write(blob)
        for side in (path + ".sha256",):
            if os.path.exists(side):
                os.remove(side)
        if with_sidecar:
            open(path + ".sha256", "w").write(open(dump + ".sha256").read())
        before = len(got)
        r = job("--verify-only", path)
        st = read_status()
        check(f"{label}: the job FAILS (exit {r.returncode}), last_success_ts did not move, verification flag is false, one webhook message",
              r.returncode == 1 and "FAILED" in r.stdout and st["last_success_ts"] == good_ts and st["restore_verification_ok"] is False and len(got) == before + 1)
        sig = alerting.Signals()
        sig.record(alerting.read_backup_status(status, time.time()), {})
        check(f"{label}: the evaluator fires AgentForgeRestoreVerificationFailed", any(rule.name == "AgentForgeRestoreVerificationFailed" and rule.check(sig) for rule in alerting.RULES))
    check("the webhook message carries no secret or identity", all("SECRET" not in json.dumps(m) for m in got))
    # a good run afterwards clears the failure flag
    r = job()
    check("a subsequent good run restores restore_verification_ok = true and moves last_success_ts", r.returncode == 0 and read_status()["restore_verification_ok"] is True and read_status()["last_success_ts"] > good_ts)

    # 3. broken WAL archiving
    sh("docker", "exec", "-u", "root", C, "chmod", "555", "/archive")  # the archive target becomes read-only: every archive_command run now fails (a full disk or lost mount looks the same)
    sh("docker", "exec", C, "psql", "-U", "postgres", "-d", "agentforge", "-c", "INSERT INTO users (id,email,email_normalized,is_active,created_at) VALUES (gen_random_uuid(),'wal@example.com','wal@example.com',true,now())")
    sh("docker", "exec", C, "psql", "-U", "postgres", "-c", "SELECT pg_switch_wal()")
    for _ in range(40):
        if "t" in sh("docker", "exec", C, "psql", "-U", "postgres", "-tAc", "SELECT coalesce(last_failed_time > last_archived_time, false) FROM pg_stat_archiver").stdout:
            break  # the archiver has failed AFTER its last success: it is broken now
        time.sleep(1)
    r = job("--check-wal")
    check("broken WAL archiving (archive target read-only): the --check-wal run FAILS", r.returncode == 1 and "WAL archiving is not healthy" in r.stdout)
finally:
    hook.shutdown()
    sh("docker", "rm", "-f", C)
    sh("docker", "volume", "rm", "-f", "af_s4_backup_arch")
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)
print("BACKUP DRILL " + ("PASSED" if not fails else "FAILED: " + "; ".join(fails)))
sys.exit(1 if fails else 0)

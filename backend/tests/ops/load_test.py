"""Authenticated, provider-bound load and reliability test (sprint 3, extended in sprint 4). Reproducible and bounded. Run from backend/ with the venv interpreter:

    python tests/ops/load_test.py --pg postgresql://postgres:<pw>@127.0.0.1:<port>/postgres --redis redis://127.0.0.1:<port>/0 [--vus 8] [--seconds 40] [--processes 2]
        [--workers 2] [--side-effects] [--mock-drop-rate 0.1] [--kill-worker-at 15] [--worker-restart-after 5]
        [--redis-container NAME --redis-stop-at 15 --redis-down-seconds 8] [--out result.json]

WHAT IT DRIVES: real HTTP against N real API processes (tests/ops/load_backend.py) and, with --workers, M real INDEPENDENT worker processes (tests/ops/load_worker.py; the API processes then run
``RUN_EXECUTION=api-only`` so ONLY workers execute runs) on a DISPOSABLE PostgreSQL and Redis it is given, with a stub model provider (controlled latency and 429 rate). With --side-effects the plan has a second
task that must send an email through the REAL ``email.send`` tool (human approval per call, auto-approved by the virtual user) to a LOCAL MOCK of Resend that counts real sends per idempotency key. Nothing talks to a
real provider and no real external side effect exists. Refuses non-loopback or default-port servers.

FAULTS: --kill-worker-at hard-kills one worker process tree mid-load (and restarts a replacement after --worker-restart-after seconds); --redis-stop-at stops the Redis container you name and starts it again after
--redis-down-seconds (it must be a DISPOSABLE container).

WHAT IT MEASURES: throughput and p50/p95/p99 latency per endpoint, error rate (HTTP 429 from the rate limiter counted separately), end-to-end plan time, queue delay, run outcomes, PostgreSQL connection pressure,
Redis pressure, the highest provider concurrency ACROSS processes, process-tree memory and CPU seconds, a rate-limit probe, and in side-effect mode the DUPLICATE AUDIT (real sends per idempotency key).
It is a regression baseline for THIS machine and THIS workload with a STUB provider; it is not a capacity statement and must not be compared with real-provider behaviour."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, BACKEND)
sys.path.insert(0, os.path.join(BACKEND, "tests"))  # tests/auth_support imports its sibling helpers as top-level modules

ap = argparse.ArgumentParser()
ap.add_argument("--pg", required=True)
ap.add_argument("--redis", required=True)
ap.add_argument("--vus", type=int, default=8)
ap.add_argument("--seconds", type=int, default=40)
ap.add_argument("--processes", type=int, default=2)
ap.add_argument("--workers", type=int, default=0, help="independent worker processes; the API processes become api-only")
ap.add_argument("--base-port", type=int, default=8741)
ap.add_argument("--global-concurrency", type=int, default=4)
ap.add_argument("--rate-429", type=float, default=0.05)
ap.add_argument("--side-effects", action="store_true")
ap.add_argument("--mock-drop-rate", type=float, default=0.0)
ap.add_argument("--kill-worker-at", type=float, default=None)
ap.add_argument("--worker-restart-after", type=float, default=5.0)
ap.add_argument("--redis-container", default=None)
ap.add_argument("--redis-stop-at", type=float, default=None)
ap.add_argument("--redis-down-seconds", type=float, default=8.0)
ap.add_argument("--out", default=None)
args = ap.parse_args()
for url, default in ((args.pg, 5432), (args.redis, 6379)):
    p = urlparse(url)
    if p.hostname not in ("127.0.0.1", "localhost") or (p.port or default) == default:
        raise SystemExit("refusing: only a disposable loopback server on a non-default port")
if args.workers == 0 and args.kill_worker_at is not None:
    raise SystemExit("--kill-worker-at needs --workers")
if args.redis_stop_at is not None and not args.redis_container:
    raise SystemExit("--redis-stop-at needs --redis-container")

from sqlalchemy import create_engine, text  # noqa: E402

from tests.auth_support import make_token, new_keypair  # noqa: E402

DBNAME = "af_load_" + uuid.uuid4().hex[:8]
admin = create_engine(args.pg, isolation_level="AUTOCOMMIT")
with admin.connect() as c:
    c.execute(text(f'CREATE DATABASE "{DBNAME}"'))
db_url = args.pg.rsplit("/", 1)[0] + "/" + DBNAME
private, public_pem = new_keypair()
pem_file = os.path.join(tempfile.gettempdir(), f"af_load_{DBNAME}.pem")
open(pem_file, "w").write(public_pem)
MOCK_PORT = args.base_port + 50

base_env = {**os.environ, "LOAD_DATABASE_URL": db_url, "LOAD_REDIS_URL": args.redis, "LOAD_PUBLIC_PEM_FILE": pem_file, "LOAD_STUB_429_RATE": str(args.rate_429), "PYTHON_DOTENV_DISABLED": "1",
            "PROVIDER_GLOBAL_CONCURRENCY": str(args.global_concurrency), "PROVIDER_CONCURRENCY": "8", "PROVIDER_ADMISSION_WAIT": "60", "METRICS_TOKEN": "load-metrics-token",
            "RATE_LIMIT_PLAN_CREATE": "15", "RATE_LIMIT_RUN_START": "600", "RATE_LIMIT_PLAN_APPROVE": "600", "RATE_LIMIT_PLAN_REVISE": "600", "RATE_LIMIT_APPROVAL_DECIDE": "600",
            "APP_ENV": "development", "GROQ_API_KEY": "stub", "DATABASE_URL": db_url, "REDIS_URL": args.redis, "RUN_EXECUTION": "api-only" if args.workers else "embedded",
            "LOAD_SIDE_EFFECTS": "1" if args.side_effects else "0", "LOAD_MOCK_RESEND_URL": f"http://127.0.0.1:{MOCK_PORT}/emails", "LOAD_AGENT": "load-analyst", "LOAD_STUB_CALLLOG": os.path.join(tempfile.gettempdir(), f"af_calls_{uuid.uuid4().hex[:8]}.log")}
subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BACKEND, env=base_env, check=True, capture_output=True)

procs, workers, ports, aux = [], [], [], []
logs = tempfile.gettempdir()


def start_worker(i):
    return subprocess.Popen([sys.executable, "-u", os.path.join(HERE, "load_worker.py")], cwd=BACKEND, env=base_env, stdout=subprocess.DEVNULL, stderr=open(os.path.join(logs, f"af_load_worker{i}.err"), "w"))


if args.side_effects:
    aux.append(subprocess.Popen([sys.executable, "-u", os.path.join(HERE, "mock_resend.py"), str(MOCK_PORT), str(args.mock_drop_rate)], cwd=BACKEND, stdout=subprocess.DEVNULL))
for i in range(args.processes):
    port = args.base_port + i
    ports.append(port)
    procs.append(subprocess.Popen([sys.executable, "-u", os.path.join(HERE, "load_backend.py"), str(port)], cwd=BACKEND, env=base_env, stdout=subprocess.DEVNULL, stderr=open(os.path.join(logs, f"af_load_{port}.err"), "w")))
for i in range(args.workers):
    workers.append(start_worker(i))


def http(method, port, path, token=None, body=None, timeout=60):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, headers=headers, data=None if body is None else json.dumps(body).encode())
    t = time.perf_counter()
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        raw = r.read()
        return r.status, (json.loads(raw) if raw else None), (time.perf_counter() - t) * 1000, dict(r.headers)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            data = None
        return e.code, data, (time.perf_counter() - t) * 1000, dict(e.headers)
    except Exception:  # noqa: BLE001
        return 0, None, (time.perf_counter() - t) * 1000, {}


def tok(i):
    return make_token(private, {"sub": str(100000000000000000000 + i), "email": f"load{i}@example.com", "jti": uuid.uuid4().hex})  # a token lives at most 600 s: a fresh one per iteration


LAT, STATUS, E2E, QDELAY, ERRORS, OUTCOMES = {}, {}, [], [], [], {}
lock = threading.Lock()
FAULTS = args.kill_worker_at is not None or args.redis_stop_at is not None or args.mock_drop_rate > 0
EVENTS = []


def rec(name, status, ms):
    with lock:
        LAT.setdefault(name, []).append(ms)
        STATUS.setdefault(name, {})
        STATUS[name][status] = STATUS[name].get(status, 0) + 1


def call(name, method, port, path, token, body=None):
    s, data, ms, hdr = http(method, port, path, token, body)
    rec(name, s, ms)
    return s, data, hdr


def wait_ready(port):
    for _ in range(120):
        if http("GET", port, "/ready")[0] == 200:
            return
        time.sleep(0.5)
    raise SystemExit(f"API on {port} did not become ready; see {os.path.join(logs, f'af_load_{port}.err')}")


def vu(i, stop_at):
    port, token = ports[i % len(ports)], tok(i)
    tools = ["math.calculate"] + (["email.send"] if args.side_effects else [])
    definition = {"name": "Load Analyst", "description": "Computes one figure.", "role": "Analyst", "purpose": "Compute figures.", "instructions": "Use the tools you are given.",
                  "model": {"provider": "groq", "name": "llama-3.3-70b-versatile"}, "allowed_tools": tools}
    s, d, _ = call("create_agent", "POST", port, "/agents", token, {"slug": "load-analyst", "definition": definition})
    if s not in (201, 409):
        ERRORS.append(("create_agent", s))
        return
    while time.time() < stop_at:
        t0, token = time.time(), tok(i)
        s, plan, _ = call("create_plan", "POST", port, "/plans", token, {"objective": f"Compute and report the monthly figure #{uuid.uuid4().hex[:6]}."})
        if s == 429:
            time.sleep(1.0)
            continue
        if s != 201:
            ERRORS.append(("create_plan", s))
            continue
        pid = plan["id"]
        s, _, _ = call("approve_plan", "POST", port, f"/plans/{pid}/approve", token, {"revision": plan["latest_revision"]})
        s, run, _ = call("start_run", "POST", port, f"/plans/{pid}/runs", token)
        if s != 202:
            ERRORS.append(("start_run", s))
            continue
        rid, final = run["id"], None
        for _ in range(300):
            s, r, _ = call("poll_run", "GET", port, f"/plan-runs/{rid}", token)
            if args.side_effects:  # the exact-action approval of the email, decided by the owner (the virtual user)
                sa, ap_, _ = call("list_approvals", "GET", port, "/approvals?status=pending", token)
                if sa == 200:
                    for item in ap_["items"]:
                        call("decide_approval", "POST", port, f"/approvals/{item['id']}/approve", token, {"payload_hash": item["payload_hash"]})
            if s == 200 and r["status"] != "running":
                final = r
                break
            time.sleep(0.4)
        outcome = final["status"] if final else "timeout"
        with lock:
            OUTCOMES[outcome] = OUTCOMES.get(outcome, 0) + 1
        if final is None or final["status"] != "succeeded":
            if not FAULTS:
                ERRORS.append(("run_not_succeeded", outcome))
            continue
        call("tool_invocations", "GET", port, f"/plan-runs/{rid}/tool-invocations", token)
        call("list_plans", "GET", port, "/plans", token)
        with lock:
            E2E.append((time.time() - t0) * 1000)
            starts = [t.get("started_at") for t in final.get("tasks", []) if t.get("started_at")]
            if starts and final.get("created_at"):
                QDELAY.append(max(0.0, (datetime.fromisoformat(min(starts)) - datetime.fromisoformat(final["created_at"])).total_seconds() * 1000))


samples = {"pg_conn": [], "pg_waiting": [], "redis_clients": [], "redis_ops": [], "slots": [], "rss_mb": []}
sampling = threading.Event()
pg_engine = create_engine(db_url, pool_size=1)
import redis as redis_lib  # noqa: E402

rc = redis_lib.Redis.from_url(args.redis, decode_responses=True, socket_connect_timeout=1, socket_timeout=1)
tick = {"n": 0}


def tree_stats(pids):
    """(working-set MB, CPU seconds) of the processes and ALL their descendants (the venv launcher spawns the real interpreter), via PowerShell/CIM; (None, None) when unavailable."""
    script = ("$ids=@(%s); $all=Get-CimInstance Win32_Process; do { $n=@($all | Where-Object { $ids -contains $_.ParentProcessId -and $ids -notcontains $_.ProcessId } | ForEach-Object { $_.ProcessId }); $ids += $n } while ($n.Count -gt 0); "
              "$m=$all | Where-Object { $ids -contains $_.ProcessId }; "
              "[math]::Round(($m | Measure-Object WorkingSetSize -Sum).Sum / 1MB, 1).ToString() + ' ' + [math]::Round((($m | Measure-Object KernelModeTime -Sum).Sum + ($m | Measure-Object UserModeTime -Sum).Sum) / 1e7, 1).ToString()" % ",".join(str(p) for p in pids))
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=25).stdout.split()
        return float(out[0]), float(out[1])
    except Exception:  # noqa: BLE001
        return None, None


def sampler():
    while not sampling.is_set():
        try:
            with pg_engine.connect() as c:
                samples["pg_conn"].append(c.execute(text("SELECT count(*) FROM pg_stat_activity WHERE datname = :d"), {"d": DBNAME}).scalar())
                samples["pg_waiting"].append(c.execute(text("SELECT count(*) FROM pg_stat_activity WHERE datname = :d AND wait_event_type = 'Lock'"), {"d": DBNAME}).scalar())
        except Exception:  # noqa: BLE001
            pass
        try:
            info = rc.info()
            samples["redis_clients"].append(info.get("connected_clients"))
            samples["redis_ops"].append(info.get("instantaneous_ops_per_sec"))
            samples["slots"].append(rc.zcard("provider:slots:groq"))
        except Exception:  # noqa: BLE001 - Redis may be down on purpose
            pass
        tick["n"] += 1
        if tick["n"] % 5 == 1:
            mem, _ = tree_stats([p.pid for p in procs + workers])
            if mem:
                samples["rss_mb"].append(mem)
        sampling.wait(1.0)


def kill_tree(proc):
    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)


KILLED_RUNS = []


def worker_fault(t_start):
    """Hard-kill (process tree, no cleanup) the worker that currently OWNS running runs (the owner id embeds the real pid: ``host:pid:uuid``), so the crash lands on in-flight work. Then start a replacement."""
    time.sleep(max(0, args.kill_worker_at - (time.time() - t_start)))
    victim, deadline = None, time.time() + 25
    while time.time() < deadline and victim is None:
        try:
            with pg_engine.connect() as c:
                rows = c.execute(text("SELECT l.owner_id, r.id FROM run_leases l JOIN plan_runs r ON r.id = l.run_id JOIN worker_processes w ON w.owner_id = l.owner_id "
                                      "WHERE r.status = 'running' AND w.kind = 'worker' ORDER BY l.owner_id")).all()
        except Exception:  # noqa: BLE001
            rows = []
        if rows:
            victim = rows[0][0]
            KILLED_RUNS.extend(str(r[1]) for r in rows if r[0] == victim)
        else:
            time.sleep(0.2)
    if victim is None:
        EVENTS.append({"t": round(time.time() - t_start, 1), "event": "NO worker owned a running run within 25 s: nothing was killed"})
        return
    pid = victim.split(":")[1]
    subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True)
    EVENTS.append({"t": round(time.time() - t_start, 1), "event": f"worker pid {pid} hard-killed while owning {len(KILLED_RUNS)} running run(s)"})
    time.sleep(args.worker_restart_after)
    workers.append(start_worker(len(workers)))
    EVENTS.append({"t": round(time.time() - t_start, 1), "event": "replacement worker started"})


def redis_fault(t_start):
    time.sleep(max(0, args.redis_stop_at - (time.time() - t_start)))
    subprocess.run(["docker", "stop", args.redis_container], capture_output=True, timeout=60)
    EVENTS.append({"t": round(time.time() - t_start, 1), "event": f"redis container {args.redis_container} stopped"})
    time.sleep(args.redis_down_seconds)
    subprocess.run(["docker", "start", args.redis_container], capture_output=True, timeout=60)
    EVENTS.append({"t": round(time.time() - t_start, 1), "event": "redis container started"})


result = {"config": {k: v for k, v in vars(args).items() if k not in ("pg", "redis", "out")} | {"stub_latency_s": 0.08, "database": "PostgreSQL (disposable, created and dropped by this script)",
                                                                                                  "redis": "disposable", "provider": "STUB"}}
try:
    for port in ports:
        wait_ready(port)
    if workers:
        time.sleep(6)  # the workers register and start polling
    cpu0 = tree_stats([p.pid for p in procs + workers])[1]
    rc.delete("load:active", "load:maxactive")
    threading.Thread(target=sampler, daemon=True).start()
    stop_at, started = time.time() + args.seconds, time.time()
    if args.kill_worker_at is not None:
        threading.Thread(target=worker_fault, args=(started,), daemon=True).start()
    if args.redis_stop_at is not None:
        threading.Thread(target=redis_fault, args=(started,), daemon=True).start()
    threads = [threading.Thread(target=vu, args=(i, stop_at)) for i in range(args.vus)]
    [t.start() for t in threads]
    [t.join(args.seconds + 240) for t in threads]
    elapsed = time.time() - started
    time.sleep(1)
    if args.redis_container:
        subprocess.run(["docker", "start", args.redis_container], capture_output=True, timeout=60)
        time.sleep(2)

    def pct(values, q):
        values = sorted(values)
        return round(values[min(len(values) - 1, int(q * len(values)))], 1) if values else None

    result["workload"] = {"elapsed_s": round(elapsed, 1), "plans_completed": len(E2E), "plans_per_minute": round(len(E2E) / elapsed * 60, 1), "e2e_ms": {"p50": pct(E2E, .5), "p95": pct(E2E, .95), "p99": pct(E2E, .99)},
                          "queue_delay_ms": {"p50": pct(QDELAY, .5), "p95": pct(QDELAY, .95), "max": round(max(QDELAY), 1) if QDELAY else None}, "run_outcomes": OUTCOMES, "errors": ERRORS[:20], "error_count": len(ERRORS),
                          "fault_events": EVENTS}
    endpoints = {}
    for name, values in LAT.items():
        st = STATUS[name]
        total = sum(st.values())
        bad = sum(n for s, n in st.items() if s not in (200, 201, 202, 409, 429))
        endpoints[name] = {"requests": total, "rps": round(total / elapsed, 1), "p50": pct(values, .5), "p95": pct(values, .95), "p99": pct(values, .99), "http_429": st.get(429, 0), "errors": bad,
                           "error_rate_pct": round(100 * bad / total, 2), "status_counts": {str(k): v for k, v in st.items() if k not in (200, 201, 202)}}
    result["endpoints"] = endpoints
    mem_end, cpu1 = tree_stats([p.pid for p in procs + workers])
    result["pressure"] = {"pg_connections_max": max(samples["pg_conn"] or [0]), "pg_waiting_on_lock_max": max(samples["pg_waiting"] or [0]), "redis_clients_max": max(samples["redis_clients"] or [0]),
                          "redis_ops_per_s_max": max(samples["redis_ops"] or [0]), "provider_slots_in_use_max": max(samples["slots"] or [0]), "process_tree_mem_mb_peak": round(max(samples["rss_mb"] or [0]), 1),
                          "cpu_seconds_used": round(cpu1 - cpu0, 1) if cpu0 is not None and cpu1 is not None else None,
                          "cpu_seconds_per_wall_second": round((cpu1 - cpu0) / elapsed, 2) if cpu0 is not None and cpu1 is not None else None}
    try:
        cross = int(rc.get("load:maxactive") or 0)  # measured INSIDE the stub with an atomic Redis counter: the real highest number of provider calls in flight at once, across all processes
    except Exception:  # noqa: BLE001
        cross = -1
    result["provider_max_concurrent_redis_counter"] = cross  # unreliable while Redis is down: kept for comparison only
    peak, active = 0, 0  # the INDEPENDENT measurement: true maximum overlap of provider calls across ALL processes, from wall-clock start/end lines
    try:
        import glob

        raw = []
        for path in glob.glob(base_env["LOAD_STUB_CALLLOG"] + ".*"):  # one file per process
            raw += [line.split() for line in open(path) if len(line.split()) == 4]
        starts_seen = {(r[2], r[3]) for r in raw if r[0] == "S"}
        ends_seen = {(r[2], r[3]) for r in raw if r[0] == "E"}
        result["calllog_unmatched_starts"] = len(starts_seen - ends_seen)  # must be 0: a start without an end would be a phantom call
        events = sorted((float(parts[1]), 1 if parts[0] == "S" else -1) for parts in raw)
        result["calllog_sample_first_violation"] = None
        live, shown = {}, False
        for parts in sorted(raw, key=lambda r: (float(r[1]), 0 if r[0] == "E" else 1)):  # ends before starts on ties
            key = (parts[2], parts[3])
            if parts[0] == "S":
                live[key] = float(parts[1])
                if len(live) > args.global_concurrency and not shown:
                    shown = True
                    result["calllog_sample_first_violation"] = {"at": float(parts[1]), "live_calls": [{"pid": k[0], "call": k[1], "started_ms_ago": round((float(parts[1]) - v) * 1000, 2)} for k, v in live.items()]}
            else:
                live.pop(key, None)
        windows, open_at = [], None
        for t_ev, d in events:
            active += d
            peak = max(peak, active)
            if active > args.global_concurrency and open_at is None:
                open_at = [t_ev, active]
            elif open_at is not None:
                open_at[1] = max(open_at[1], active)
                if active <= args.global_concurrency:
                    windows.append({"from_s": round(open_at[0] - started, 2), "to_s": round(t_ev - started, 2), "peak": open_at[1]})
                    open_at = None
        result["provider_calls_logged"] = len(events) // 2
        result["concurrency_over_cap_windows"] = windows[:12]  # seconds since the workload started, to be compared with fault_events
    except OSError:
        peak = -1
    result["provider_max_concurrent_across_processes"] = peak
    result["provider_global_cap_respected"] = 0 <= peak <= args.global_concurrency
    stub = []
    for port in ports:
        s, d, _, _ = http("GET", port, "/_stub_stats")
        stub.append(d)
    result["provider_stub_per_api_process"] = stub
    metrics = {}
    for port in ports:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/metrics", headers={"Authorization": "Bearer load-metrics-token"})
        try:
            text_body = urllib.request.urlopen(req, timeout=20).read().decode()
        except Exception:  # noqa: BLE001
            continue
        for line in text_body.splitlines():
            if line.startswith(("agentforge_provider_events_total", "agentforge_rate_limited_total", "agentforge_runs_finished_total", "agentforge_redis_unavailable_total", "agentforge_lease_takeovers_total")):
                metrics[f"{port}:{line.rsplit(' ', 1)[0]}"] = float(line.rsplit(" ", 1)[1])
    result["metrics_counters"] = metrics

    # DATABASE AUDIT: the durable truth of what ran (the same whichever process executed it)
    with pg_engine.connect() as c:
        result["db_audit"] = {
            "runs_by_status": dict(c.execute(text("SELECT status, count(*) FROM plan_runs GROUP BY status")).all()),
            "run_leases_with_generation_gt_1_takeovers": c.execute(text("SELECT count(*) FROM run_leases WHERE generation > 1")).scalar(),
            "executions_still_running": c.execute(text("SELECT count(*) FROM agent_executions WHERE status = 'running'")).scalar(),
            "runs_still_running": c.execute(text("SELECT count(*) FROM plan_runs WHERE status = 'running'")).scalar(),
            "dispatches_by_state": dict(c.execute(text("SELECT state, count(*) FROM tool_dispatches GROUP BY state")).all()),
            "operations_with_more_than_one_succeeded_dispatch": c.execute(text("SELECT count(*) FROM (SELECT operation_key FROM tool_dispatches WHERE state IN ('succeeded','reconciled_succeeded') GROUP BY operation_key HAVING count(*) > 1) x")).scalar(),
            "dispatches_by_task_key": dict(c.execute(text("SELECT coalesce(task_key, '-'), count(*) FROM tool_dispatches GROUP BY 1")).all()),
            "invocations_by_tool_and_status": [list(r) for r in c.execute(text("SELECT tool_id, status, count(*) FROM tool_invocations GROUP BY 1, 2 ORDER BY 1, 2")).all()],
            "killed_runs_final_status": ({} if not KILLED_RUNS else dict(c.execute(text("SELECT status, count(*) FROM plan_runs WHERE id = ANY(CAST(:ids AS uuid[])) GROUP BY status"), {"ids": KILLED_RUNS}).all())),
            "task_error_codes": dict(c.execute(text("SELECT coalesce(error_code, 'none'), count(*) FROM plan_task_runs GROUP BY 1")).all()),
        }
    if args.side_effects:
        s, d, _, _ = http("GET", MOCK_PORT, "/_stats")
        states = result["db_audit"]["dispatches_by_state"]
        result["side_effect_audit"] = {"mock_resend": d, "dispatch_states": states, "no_duplicate_real_sends": bool(d) and d["duplicates"] == 0 and d["max_per_key"] <= 1,
                                       "real_sends_not_more_than_dispatches_that_started": bool(d) and d["real_sends"] <= sum(states.get(k, 0) for k in ("succeeded", "unknown", "reconciled_succeeded", "dispatching", "failed"))}

    # RATE-LIMIT PROBE: one user fires 60 plan creations spread over ALL API processes; limit 15/min shared through Redis (skipped while a Redis fault is being injected)
    if args.redis_stop_at is None:
        rem = 60 - time.time() % 60
        if rem < 25:  # start early in a window: a fixed window admits up to 2x the limit when a probe straddles a boundary (D070)
            time.sleep(rem + 1)
        probe_token, ok, limited, retry = tok(9999), 0, 0, []
        for n in range(60):
            s, d, ms, hdr = http("POST", ports[n % len(ports)], "/plans", probe_token, {"objective": "probe"})
            if s == 429:
                limited += 1
                retry.append({k.lower(): v for k, v in hdr.items()}.get("retry-after"))
            elif s in (201, 422, 409):
                ok += 1
        result["rate_limit_probe"] = {"limit_per_minute": 15, "requests": 60, "admitted": ok, "refused_429": limited, "retry_after_seen": sorted(set(r for r in retry if r))[:3], "processes": len(ports),
                                      "shared_budget_respected": ok <= 15}
finally:
    sampling.set()
    for p in procs + workers + aux:
        kill_tree(p)
    for p in procs + workers + aux:
        try:
            p.wait(15)
        except Exception:  # noqa: BLE001
            pass
    pg_engine.dispose()
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{DBNAME}" WITH (FORCE)'))
    try:
        rc.flushdb()
    except Exception:  # noqa: BLE001
        pass
    import glob as _g

    for junk in [pem_file] + _g.glob(base_env["LOAD_STUB_CALLLOG"] + ".*"):
        try:
            os.remove(junk)
        except OSError:
            pass

print(json.dumps(result, indent=1))
if args.out:
    json.dump(result, open(args.out, "w"), indent=1)

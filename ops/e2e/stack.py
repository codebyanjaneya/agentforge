"""Isolated, test-only stack for browser acceptance: real FastAPI app + real Next server, throw-away SQLite, throw-away keys, scripted (demo) model.

    python ops/e2e/stack.py start     # from the repository root; prints nothing secret
    python ops/e2e/stack.py stop

Safety: APP_ENV=test (SQLite is refused otherwise), AGENTFORGE_DEMO_MODE=1 (scripted model, simulated send: no provider call, no email), ports 3040/8040 only
(never 3010/8010), keys and NEXTAUTH_SECRET generated fresh into ops/e2e/.run (git-ignored). Nothing here is read by, or changes, the production sign-in code:
the browser gets a NextAuth session cookie encrypted with the throw-away secret, which the unmodified app decodes and exchanges for an ES256 token that the
unmodified backend verifies against the throw-away public key. A stack started with different keys cannot honour these cookies."""
import json, os, secrets, subprocess, sys, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUN = Path(__file__).resolve().parent / ".run"
API_PORT, WEB_PORT = 8040, 3040
assert API_PORT not in (8010,) and WEB_PORT not in (3010,)
NPX = "npx.cmd" if os.name == "nt" else "npx"


def up(url: str) -> bool:
    try:
        urllib.request.urlopen(url, timeout=2)
        return True
    except Exception as exc:  # noqa: BLE001
        return getattr(exc, "code", None) is not None  # an HTTP error still means a server answered


def _spawn_api(py, env, slow=False):
    cmd = [str(py), str(Path(__file__).resolve().parent / "slow_main.py"), str(API_PORT)] if slow else [str(py), "-m", "uvicorn", "main:app", "--port", str(API_PORT)]
    return subprocess.Popen(cmd, cwd=ROOT / "backend", env=env, stdout=open(RUN / "api.log", "a"), stderr=subprocess.STDOUT)


def restart_api() -> None:
    """Hard-kill ONLY the API process this script started and start it again (normal speed, same database and keys): a real crash and recovery for the journeys."""
    pids = json.loads((RUN / "pids.json").read_text())
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pids["api"])], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) if os.name == "nt" else os.kill(pids["api"], 9)
    time.sleep(1)
    py = ROOT / "backend" / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    p = _spawn_api(py, {**os.environ, **json.loads((RUN / "api.env.json").read_text())}, slow="--slow-send" in sys.argv)
    pids["api"] = p.pid
    (RUN / "pids.json").write_text(json.dumps(pids))
    for _ in range(40):
        if up(f"http://127.0.0.1:{API_PORT}/health"):
            print("api restarted")
            return
        time.sleep(1)
    sys.exit("api did not come back")


def start() -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    if up(f"http://127.0.0.1:{API_PORT}/health") or up(f"http://127.0.0.1:{WEB_PORT}/"):
        sys.exit("ports 8040/3040 are already in use; run `stop` first")
    RUN.mkdir(exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    priv = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    pub = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    cfg = {"nextauth_secret": secrets.token_urlsafe(32), "private_key": priv, "kid": "e2e-1", "api": f"http://localhost:{API_PORT}", "web": f"http://localhost:{WEB_PORT}"}
    (RUN / "config.json").write_text(json.dumps(cfg))
    db = RUN / "e2e.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    backend_env = {**os.environ, "APP_ENV": "test", "AGENTFORGE_DEMO_MODE": "1", "GROQ_API_KEY": "placeholder-not-a-key", "DATABASE_URL": f"sqlite:///{db.as_posix()}",
                   "AUTH_JWT_PUBLIC_KEYS": json.dumps({cfg["kid"]: pub}), "AUTH_CONFIG_REQUIRED": "1", "CORS_ALLOWED_ORIGINS": cfg["web"], "LOG_FORMAT": "text",
                   "TOKEN_ISSUE_PER_MINUTE": "600", "PROVIDER_CONCURRENCY": "1"}
    py = ROOT / "backend" / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run([str(py), "-m", "alembic", "upgrade", "head"], cwd=ROOT / "backend", env=backend_env, check=True, stdout=subprocess.DEVNULL)
    web_env = {**os.environ, "NEXTAUTH_SECRET": cfg["nextauth_secret"], "NEXTAUTH_URL": cfg["web"], "BACKEND_TOKEN_PRIVATE_KEY": priv, "BACKEND_TOKEN_KID": cfg["kid"],
               "NEXT_PUBLIC_API_BASE": cfg["api"], "GOOGLE_CLIENT_ID": "unused-in-e2e", "GOOGLE_CLIENT_SECRET": "unused-in-e2e", "TOKEN_ISSUE_PER_MINUTE": "600"}
    (RUN / "web.env.json").write_text(json.dumps({k: web_env[k] for k in ("NEXTAUTH_SECRET", "NEXTAUTH_URL", "BACKEND_TOKEN_PRIVATE_KEY", "BACKEND_TOKEN_KID", "NEXT_PUBLIC_API_BASE")}))
    if "--skip-build" not in sys.argv:
        subprocess.run([NPX, "next", "build"], cwd=ROOT / "frontend", env=web_env, check=True, stdout=subprocess.DEVNULL)
    procs = {}
    (RUN / "api.env.json").write_text(json.dumps({k: v for k, v in backend_env.items() if k in ("APP_ENV", "AGENTFORGE_DEMO_MODE", "GROQ_API_KEY", "DATABASE_URL", "AUTH_JWT_PUBLIC_KEYS", "AUTH_CONFIG_REQUIRED", "CORS_ALLOWED_ORIGINS", "LOG_FORMAT", "TOKEN_ISSUE_PER_MINUTE", "PROVIDER_CONCURRENCY")}))
    procs["api"] = _spawn_api(py, backend_env, slow="--slow-send" in sys.argv)
    procs["web"] = subprocess.Popen([NPX, "next", "start", "-p", str(WEB_PORT)], cwd=ROOT / "frontend", env=web_env, stdout=open(RUN / "web.log", "w"), stderr=subprocess.STDOUT)
    (RUN / "pids.json").write_text(json.dumps({k: p.pid for k, p in procs.items()}))
    for _ in range(60):
        if up(f"http://127.0.0.1:{API_PORT}/health") and up(f"http://127.0.0.1:{WEB_PORT}/"):
            print("e2e stack up: api", API_PORT, "web", WEB_PORT)
            return
        time.sleep(1)
    sys.exit("stack did not come up; see ops/e2e/.run/*.log")


def stop() -> None:
    f = RUN / "pids.json"
    if f.exists():
        for pid in json.loads(f.read_text()).values():  # only the pids this script started
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)] if os.name == "nt" else ["kill", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        f.unlink()
    for name in ("config.json", "web.env.json", "api.env.json"):  # the throw-away secrets go with the stack
        (RUN / name).unlink(missing_ok=True)


if __name__ == "__main__":
    {"start": start, "stop": stop, "restart-api": restart_api}[sys.argv[1]]()

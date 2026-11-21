"""Client side of the Redis Sentinel failover drill (sprint 4, extended in 4.1). Runs INSIDE the drill's Docker network (Sentinel announces container addresses, which the host cannot reach). It drives the real
``redis_client.build`` (Sentinel-aware client) and the real ``agentplatform.coordination.Coordinator`` (with replica acknowledgement, the Sentinel configuration) and writes one JSON line per observation to
/out/timeline.jsonl.

THE INVARIANT UNDER TEST. The global cap here is 1. At t=0 the client takes the ONLY slot with a 60 s lease and treats it as a request that keeps running (it never releases it). From then on, a granted
admission while that holder is still running means the cap of 1 was exceeded. A failover may lose the holder's lease on the promoted master (replication is asynchronous); the coordinator must then refuse
admission (``closed``) until the lost lease could no longer be running, instead of granting a second slot. ``degrade`` is expected to violate it (that is what the opt-in means).

  python client_loop.py <seconds> <degrade|closed> <sentinel host:port,...> <master name>"""

import json
import sys
import time

sys.path.insert(0, "/app")

import redis_client  # noqa: E402
from agentplatform.coordination import Coordinator  # noqa: E402

seconds, mode, sentinels, master = float(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
LEASE = 60
client = redis_client.build("redis://sentinel-managed/0", tuple((h, int(p)) for h, p in (x.rsplit(":", 1) for x in sentinels.split(","))), master)
coord = Coordinator(client, fail_closed=(mode == "closed"), replica_ack_ms=100)
out = open("/out/timeline.jsonl", "w", buffering=1)
t0 = time.time()
held = coord.acquire("drill", 1, LEASE)  # the one running request that owns the only slot
out.write(json.dumps({"t": 0, "kind": "held_lease_taken", "ok": held is not None, "token": held, "lease_seconds": LEASE}) + "\n")
while time.time() - t0 < seconds:
    now = round(time.time() - t0, 2)
    try:
        client.incr("drill:probe")
        probe = client.execute_command("ROLE")[0]
    except Exception as exc:  # noqa: BLE001
        probe = "ERR:" + type(exc).__name__
    try:
        token = coord.acquire("drill", 1, LEASE)
        if token is not None:
            coord.release("drill", token)
    except Exception as exc:  # noqa: BLE001 - the coordinator must swallow store errors: record the type if it does not
        token, probe = None, probe + "|UNEXPECTED:" + type(exc).__name__
    out.write(json.dumps({"t": now, "kind": "tick", "probe": probe, "slot_granted": token is not None, "cap_enforced_gauge": int(coord.enforced)}) + "\n")
    time.sleep(0.1)
state = {"t": round(time.time() - t0, 2), "kind": "final_state"}
try:
    state.update(run_id=client.info("server")["run_id"], role=client.execute_command("ROLE")[0], origin=client.get("provider:origin"),
                 quarantine_ttl_ms=client.pttl("provider:quarantine"), held_lease_present=bool(client.zscore("provider:slots:drill", held)) if held else False)
except Exception as exc:  # noqa: BLE001
    state["error"] = type(exc).__name__
out.write(json.dumps(state) + "\n")
out.close()

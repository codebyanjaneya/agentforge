#!/usr/bin/env bash
# Redis Sentinel failover drill on DISPOSABLE Docker resources (sprint 4, rewritten in 4.1). Creates and removes only the network/containers named below.
#   bash ops/sentinel_failover_drill.sh [closed|degrade] [ip|hostname] [lose-lease]
# Topology: 1 master + 1 replica + 3 sentinels (quorum 2, down-after 5 s). A client container drives the real Sentinel-aware client and the real provider-coordination code with a global cap of 1: it takes
# the ONLY slot (60 s lease, replica-acknowledged) and keeps that "request" running, then the host stops the MASTER. The drill checks:
#   * before the failover: the replica HOLDS the lease (replication caught up); the optional ``lose-lease`` argument deletes it from the replica just before the failover to simulate the asynchronous-replication gap;
#   * Sentinel promotes the replica and the client reconnects through it (measured outage);
#   * where the lease is afterwards, whether the coordinator detected the server move (origin / quarantine keys), and THE INVARIANT: no admission is granted while the original holder could still be running
#     (``closed``: 0 violations required; ``degrade``: violations are EXPECTED and counted: that is what the opt-in means);
#   * when normal admission resumes.
# ``ip`` monitors the master by IP address; ``hostname`` monitors it by NAME with a static /etc/hosts entry in every sentinel (without it a stopped container's name stops resolving and Sentinel's blocking DNS
# lookups keep it in TILT: observed in sprint 4). NOT proven: Cluster, cross-AZ partitions, split brain, the legacy short-term memory client.
set -euo pipefail
export MSYS_NO_PATHCONV=1
MODE=${1:-closed}; ADDR=${2:-ip}; LOSE=${3:-no}
NET=af-s4-sent; MASTER=af-s4-rm; REPLICA=af-s4-rr; DURATION=130
HERE="$(cd "$(dirname "$0")" && pwd -W 2>/dev/null || pwd)"; BACKEND="$(cd "$HERE/../backend" && pwd -W 2>/dev/null || (cd "$HERE/../backend" && pwd))"; OUT="$HERE/sentinel_drill/out"
mkdir -p "$OUT"; rm -f "$OUT/timeline.jsonl"
cleanup() { docker rm -f $MASTER $REPLICA af-s4-s1 af-s4-s2 af-s4-s3 af-s4-client >/dev/null 2>&1 || true; docker network rm $NET >/dev/null 2>&1 || true; }
trap cleanup EXIT; cleanup
docker network create $NET >/dev/null
# Redis is run the way the docs recommend for this policy: the master refuses writes when no replica is within 5 s (bounds what a failover can lose)
docker run -d --name $MASTER --network $NET redis:7-alpine redis-server --appendonly no --min-replicas-to-write 1 --min-replicas-max-lag 5 >/dev/null
MIP=$(docker inspect -f '{{(index .NetworkSettings.Networks "'$NET'").IPAddress}}' $MASTER)
docker run -d --name $REPLICA --network $NET redis:7-alpine redis-server --replicaof $MIP 6379 >/dev/null
RIP=$(docker inspect -f '{{(index .NetworkSettings.Networks "'$NET'").IPAddress}}' $REPLICA)
if [ "$ADDR" = "hostname" ]; then MON="$MASTER"; HOSTS="--add-host $MASTER:$MIP --add-host $REPLICA:$RIP"; RESOLVE="sentinel resolve-hostnames yes
sentinel announce-hostnames yes"; else MON="$MIP"; HOSTS=""; RESOLVE=""; fi
CONF="port 26379
$RESOLVE
sentinel monitor agentforge $MON 6379 2
sentinel down-after-milliseconds agentforge 5000
sentinel failover-timeout agentforge 15000
sentinel parallel-syncs agentforge 1"
for i in 1 2 3; do docker run -d --name af-s4-s$i --network $NET $HOSTS redis:7-alpine sh -c "printf '%s\n' \"$CONF\" > /tmp/s.conf && redis-server /tmp/s.conf --sentinel" >/dev/null; done
for _ in $(seq 30); do [ "$(docker exec af-s4-s1 redis-cli -p 26379 sentinel master agentforge 2>/dev/null | grep -A1 num-slaves | tail -1)" = "1" ] && break; sleep 1; done
echo "addressing=$ADDR mode=$MODE; sentinel sees master: $(docker exec af-s4-s1 redis-cli -p 26379 sentinel get-master-addr-by-name agentforge | tr '\n' ' ') (master $MIP, replica $RIP)"
docker run -d --name af-s4-client --network $NET -v "$BACKEND":/app:ro -v "$HERE/sentinel_drill":/drill:ro -v "$OUT":/out python:3.11-slim sh -c "pip install -q redis >/dev/null 2>&1 && python /drill/client_loop.py $DURATION $MODE af-s4-s1:26379,af-s4-s2:26379,af-s4-s3:26379 agentforge" >/dev/null
for _ in $(seq 90); do [ -s "$OUT/timeline.jsonl" ] && break; sleep 1; done
sleep 8
TOKEN=$(python -c "import json;print(json.loads(open(r'$OUT/timeline.jsonl').readline())['token'])")
echo "BEFORE the failover: the lease token is on the replica: $(docker exec $REPLICA redis-cli zscore provider:slots:drill "$TOKEN" | grep -qE '^[0-9]' && echo YES || echo NO); replica role: $(docker exec $REPLICA redis-cli role | head -1)"
if [ "$LOSE" = "lose-lease" ]; then docker exec $REPLICA redis-cli config set replica-read-only no >/dev/null; docker exec $REPLICA redis-cli del provider:slots:drill >/dev/null; echo "LOSE-LEASE variant: the holder's slot was deleted from the replica just before the failover (simulates the asynchronous-replication gap; the origin key stays)"; fi
docker stop -t 0 $MASTER >/dev/null; echo "master stopped at $(date -u +%H:%M:%S)"
NEW=""; for _ in $(seq 90); do NEW=$(docker exec af-s4-s1 redis-cli -p 26379 sentinel get-master-addr-by-name agentforge | head -1); [ "$NEW" = "$RIP" ] && break; sleep 1; done
echo "AFTER the failover Sentinel reports master $NEW (the old replica $RIP): promoted=$([ "$NEW" = "$RIP" ] && echo yes || echo NO)"
echo "promoted node: lease present=$(docker exec $REPLICA redis-cli zscore provider:slots:drill "$TOKEN" | grep -qE '^[0-9]' && echo YES || echo NO), origin=$(docker exec $REPLICA redis-cli get provider:origin | cut -c1-12)..., quarantine ttl ms=$(docker exec $REPLICA redis-cli pttl provider:quarantine), run_id=$(docker exec $REPLICA redis-cli info server | grep run_id | tr -d '\r' | cut -c1-20)"
for _ in $(seq 120); do [ "$(docker inspect -f '{{.State.Running}}' af-s4-client)" = "false" ] && break; sleep 2; done
docker logs af-s4-client 2>&1 | grep -v "^$" | tail -n 3
python - "$OUT/timeline.jsonl" "$MODE" <<'PYEOF'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1])]
held = rows[0]
ticks = [r for r in rows if r["kind"] == "tick"]
err = [r for r in ticks if str(r["probe"]).startswith("ERR")]
first_err = err[0]["t"] if err else None
after = [r for r in ticks if first_err is not None and r["t"] > first_err and r["probe"].startswith("master")]
recovered = after[0]["t"] if after else None
hold_end = held["lease_seconds"]  # the held request keeps running for its whole lease
viol = [r for r in ticks if r["slot_granted"] and r["t"] <= hold_end]
granted_after = [r["t"] for r in ticks if r["slot_granted"] and r["t"] > hold_end]
print("held request owns the only slot from t=0 to t=%ss (cap = 1)" % hold_end)
print("first probe error at t=%s s | first success through the promoted master at t=%s s | measured outage (client view) %s s" % (first_err, recovered, round(recovered - first_err, 1) if recovered and first_err else None))
print("admissions granted while the original holder could still be running (cap violations): %d" % len(viol), ("(first at t=%s s)" % viol[0]["t"]) if viol else "")
print("normal admission resumed at t=%s s" % (granted_after[0] if granted_after else "never within the run"))
final = [r for r in rows if r["kind"] == "final_state"]
print("final state on the master the client uses:", {k: v for k, v in (final[0] if final else {}).items() if k != "kind"})
ok = bool(err) and recovered is not None and (len(viol) == 0 if sys.argv[2] == "closed" else len(viol) > 0)
print("SENTINEL DRILL PASSED" if ok else "SENTINEL DRILL FAILED")
sys.exit(0 if ok else 1)
PYEOF

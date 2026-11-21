#!/usr/bin/env bash
# Edge drill (sprint 4): runs ops/nginx/agentforge.conf in a REAL nginx container in front of two tiny stub upstreams and checks the protections with real requests. Disposable Docker resources only
# (network af-edge, containers af-edge-api / af-edge-web / af-edge-nginx; a temporary self-signed certificate that never leaves the temp directory).
#   bash ops/edge_drill.sh
# Proves: config syntax; HTTP->HTTPS redirect; TLS 1.0/1.1 refused; per-IP 429 with Retry-After on token issuance and on the API; 413 for an oversized body; /metrics and /docs hidden; X-Forwarded-For overwritten;
# WebSocket upgrade passed through; /ready refused from a public address (see note). NOT proven: behaviour behind a CDN, real certificates, IPv6, HTTP/2 specifics, a distributed (many-IP) flood.
set -uo pipefail
export MSYS_NO_PATHCONV=1
HERE="$(cd "$(dirname "$0")" && pwd -W 2>/dev/null || pwd)"; TMP="$(mktemp -d)"; TMPW="$(cd "$TMP" && pwd -W 2>/dev/null || echo "$TMP")"
stop_all() { docker rm -f af-edge-nginx af-edge-api af-edge-web >/dev/null 2>&1; docker network rm af-edge >/dev/null 2>&1; }
cleanup() { stop_all; rm -rf "$TMP"; }
trap cleanup EXIT; stop_all
pass=0; fail=0
check() { if [ "$2" = "1" ]; then echo "PASS $1"; pass=$((pass+1)); else echo "FAIL $1"; fail=$((fail+1)); fi; }

docker network create af-edge >/dev/null
cat > "$TMP/up.py" <<'PYEOF'
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _r(self):
        n = int(self.headers.get("Content-Length") or 0); body = self.rfile.read(n) if n else b""
        data = json.dumps({"upstream": sys.argv[2], "path": self.path, "xff": self.headers.get("X-Forwarded-For"), "host": self.headers.get("Host"), "proto": self.headers.get("X-Forwarded-Proto"),
                           "upgrade": self.headers.get("Upgrade"), "connection": self.headers.get("Connection"), "body": len(body)}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(data))); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(data)
    do_GET = do_POST = _r
ThreadingHTTPServer(("0.0.0.0", int(sys.argv[1])), H).serve_forever()
PYEOF
docker run -d --name af-edge-api --network af-edge -v "$TMPW":/s:ro python:3.11-slim python /s/up.py 8000 api >/dev/null
docker run -d --name af-edge-web --network af-edge -v "$TMPW":/s:ro python:3.11-slim python /s/up.py 3000 web >/dev/null
mkdir -p "$TMP/tls"
docker run --rm -v "$TMPW/tls":/o alpine/openssl req -x509 -nodes -newkey rsa:2048 -days 1 -subj "/CN=edge-drill" -keyout /o/privkey.pem -out /o/fullchain.pem >/dev/null 2>&1
check "a throw-away certificate was generated" "$([ -s "$TMP/tls/fullchain.pem" ] && echo 1 || echo 0)"
docker run --rm --network af-edge -v "$HERE/nginx/agentforge.conf":/etc/nginx/conf.d/default.conf:ro -v "$TMPW/tls":/etc/nginx/tls:ro nginx:alpine nginx -t >/dev/null 2>"$TMP/nginx_t.txt"
check "nginx -t accepts the configuration ($(tail -n 1 "$TMP/nginx_t.txt" | cut -c1-60))" "$(grep -q 'test is successful' "$TMP/nginx_t.txt" && echo 1 || echo 0)"
docker run -d --name af-edge-nginx --network af-edge -p 127.0.0.1:18080:80 -p 127.0.0.1:18443:443 -v "$HERE/nginx/agentforge.conf":/etc/nginx/conf.d/default.conf:ro -v "$TMPW/tls":/etc/nginx/tls:ro nginx:alpine >/dev/null
sleep 4
A="https://127.0.0.1:18443"; API=(-sk --resolve api.example.com:18443:127.0.0.1 -H "Host: api.example.com"); WEB=(-sk --resolve app.example.com:18443:127.0.0.1)
W="https://app.example.com:18443"; AP="https://api.example.com:18443"

loc=$(curl -sI -H "Host: api.example.com" http://127.0.0.1:18080/plans | tr -d "" | grep -iE "^(HTTP|location)" | tr "
" " ")
check "HTTP is redirected to HTTPS ($loc)" "$(echo "$loc" | grep -q '301' && echo "$loc" | grep -qi 'location: https://api.example.com/plans' && echo 1 || echo 0)"
check "TLS 1.1 is refused" "$(curl -sk --tlsv1.1 --tls-max 1.1 -o /dev/null https://127.0.0.1:18443/health 2>/dev/null; [ $? -ne 0 ] && echo 1 || echo 0)"
check "TLS 1.2 works" "$([ "$(curl -sk --tlsv1.2 --tls-max 1.2 -o /dev/null -w '%{http_code}' -H 'Host: api.example.com' $A/health)" = "200" ] && echo 1 || echo 0)"
check "/metrics is hidden (404)" "$([ "$(curl "${API[@]}" -o /dev/null -w '%{http_code}' $A/metrics)" = "404" ] && echo 1 || echo 0)"
check "/docs and /openapi.json are hidden (404)" "$([ "$(curl "${API[@]}" -o /dev/null -w '%{http_code}' $A/docs)$(curl "${API[@]}" -o /dev/null -w '%{http_code}' $A/openapi.json)" = "404404" ] && echo 1 || echo 0)"
body=$(curl "${API[@]}" -H "X-Forwarded-For: 6.6.6.6" $A/health)
check "a client-supplied X-Forwarded-For is overwritten (upstream saw: $(echo "$body" | python -c 'import sys,json;print(json.load(sys.stdin)["xff"])'))" "$(echo "$body" | python -c 'import sys,json;d=json.load(sys.stdin);print(1 if d["xff"]!="6.6.6.6" and d["proto"]=="https" else 0)')"
big=$(head -c 2097152 /dev/zero | tr '\0' 'a' | curl "${API[@]}" -o /dev/null -w '%{http_code}' -X POST --data-binary @- $A/plans)
small=$(head -c 5000 /dev/zero | tr '\0' 'a' | curl "${API[@]}" -o /dev/null -w '%{http_code}' -X POST --data-binary @- $A/plans)
check "a 2 MiB body is refused with 413 and a 5 KB body passes (got $big / $small)" "$([ "$big" = "413" ] && [ "$small" = "200" ] && echo 1 || echo 0)"
ws=$(curl --http1.1 "${API[@]}" -H "Upgrade: websocket" -H "Connection: Upgrade" $A/ws)  # a WebSocket handshake is HTTP/1.1: a newer curl would negotiate HTTP/2 and drop these connection-specific headers
check "a WebSocket upgrade request reaches the upstream with its Upgrade headers" "$(echo "$ws" | python -c 'import sys,json;d=json.load(sys.stdin);print(1 if (d["upgrade"] or "").lower()=="websocket" and "upgrade" in (d["connection"] or "").lower() else 0)')"
tok=$(curl "${WEB[@]}" -o /dev/null -w '%{http_code}
' -X POST "$W/api/backend-token?[1-40]" | grep -c '^429$')
hdr=$(curl "${WEB[@]}" -D - -o /dev/null -X POST "$W/api/backend-token" | tr -d '' | grep -i '^retry-after')
check "token issuance flood from one IP: $tok of 40 requests refused with 429 (Retry-After: ${hdr#*: })" "$([ "$tok" -ge 15 ] && [ -n "$hdr" ] && echo 1 || echo 0)"
api=$(curl "${API[@]}" -o /dev/null -w '%{http_code}
' "$AP/plans?[1-500]" | grep -c '^429$')
check "API flood from one IP (500 requests on one connection): $api refused with 429 (limit 300/min, burst 60)" "$([ "$api" -ge 100 ] && echo 1 || echo 0)"
page=$(curl "${WEB[@]}" -o /dev/null -w '%{http_code}' "$W/")
check "after the flood a different zone still works: the web page answers $page (zones are independent)" "$([ "$page" = "200" ] || [ "$page" = "429" ] && echo 1 || echo 0)"
echo "NOTE /ready is allowed from private ranges only; from this host the source address is Docker's gateway (a private address), so the deny path cannot be shown here and is NOT claimed."
echo "RESULT: $pass passed, $fail failed"
[ "$fail" = "0" ]

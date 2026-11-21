"""A LOCAL mock of the Resend send-email endpoint for the load/reliability tests (sprint 4). It models ONLY what the provider documents about ``Idempotency-Key`` (same key + same payload = the original
response, never a second send) and counts what really happened, so a test can prove "no duplicate external side effect": ``/_stats`` returns the number of REAL sends and the per-key send counts.

    python tests/ops/mock_resend.py <port> [drop_rate]
``drop_rate`` (0..1): the fraction of NEW sends whose connection is closed after the send was recorded (an accepted request whose answer is lost: the unknown-outcome case). Loopback only."""

import json
import random
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

port, drop = int(sys.argv[1]), float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
lock = threading.Lock()
SENT = {}  # idempotency key -> number of REAL sends
BODIES, IDS = {}, {}
REQUESTS = {"n": 0}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/_stats":
            with lock:
                return self.reply(200, {"requests": REQUESTS["n"], "real_sends": sum(SENT.values()), "keys": len(SENT), "duplicates": sum(1 for v in SENT.values() if v > 1),
                                        "max_per_key": max(SENT.values() or [0])})
        self.reply(404, {"name": "not_found"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        key = self.headers.get("Idempotency-Key")
        if not key or not 1 <= len(key) <= 256:
            return self.reply(400, {"name": "invalid_idempotency_key"})
        with lock:
            REQUESTS["n"] += 1
            if key in IDS:
                if BODIES[key] != body:
                    return self.reply(409, {"name": "invalid_idempotent_request"})
                return self.reply(200, {"id": IDS[key]})  # documented: the original response, nothing sent again
            SENT[key] = SENT.get(key, 0) + 1
            IDS[key] = f"mock-{len(IDS) + 1:08d}"
            BODIES[key] = body
            email_id = IDS[key]
        if random.random() < drop:
            self.close_connection = True
            self.connection.close()  # accepted and recorded, but the caller never gets the answer
            return
        self.reply(200, {"id": email_id})


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()

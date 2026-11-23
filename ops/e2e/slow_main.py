"""HARNESS ONLY (never imported by the application): serves the real app with the SIMULATED send taking ~25 s, so a journey can hard-kill the process while a side effect is
in flight and observe the real recovery (an UNKNOWN outcome). Only the simulated demo tool is slowed; nothing else is patched. Usage: slow_main.py <port>"""
import sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from agentplatform.tools import fixtures  # noqa: E402

_real = fixtures.send_note


def slow_send_note(args, dispatch=None):
    time.sleep(25)
    return _real(args, dispatch)


fixtures.send_note = slow_send_note  # register_demo_tools reads the module attribute when main.py builds the registry

import uvicorn  # noqa: E402

if __name__ == "__main__":
    uvicorn.run("main:app", port=int(sys.argv[1]), log_level="info")

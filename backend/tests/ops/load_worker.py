"""Load-test WORKER process (sprint 4): the real ``worker.py`` (same ownership/lease/fencing code, no HTTP port) with the stub model provider and, when asked, the email tool against the local mock.
Use with API processes started with ``RUN_EXECUTION=api-only``. Same safety checks as the API launcher.

    python tests/ops/load_worker.py"""

import load_stub

load_stub.prepare_environment()

import main  # noqa: E402
import worker  # noqa: E402

load_stub.install(main)
worker.main()

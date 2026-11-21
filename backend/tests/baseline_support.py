"""Test-side access to the frozen baseline schema (revision 0001_baseline).

Since M1-A.1 the single source is the production module ``migrations.baseline_schema`` (the
pre-flight needs it at runtime and production code must never import ``tests``). This module only
re-exports it, plus a ``Base``-like stand-in for tests. The source is verified independently of the
live models: against the T1-A plan DDL (tests/test_models_metadata.py) and against what the baseline
migration really creates (tests/test_baseline_revision.py).
"""

from types import SimpleNamespace

from migrations.baseline_schema import BASELINE_REVISION, BASELINE_TABLES, baseline_metadata  # noqa: F401


def baseline_base() -> SimpleNamespace:
    """A stand-in for ``memory.models.Base`` exposing only the frozen baseline ``metadata``."""
    return SimpleNamespace(metadata=baseline_metadata())

"""The trusted auto-run allowlist (M4-C finalization): the ONLY tool versions that may run without a human approval.

Adding a line here is a code change that is reviewed and released; it is not configurable at runtime, by a tool's metadata, by an agent definition or by anything
a model says. A new VERSION of a tool is a different entry: it needs approval until it is listed. Five shipped tools are pure, read-only computations; the sixth (calendar.public_holidays) is a read-only lookup at one fixed public host."""

from agentplatform.tools.registry import ToolPolicy

AUTO_RUN = frozenset({
    "math.calculate@1",
    "data.summarize_csv@1",
    "finance.unit_economics@1",
    "time.date_math@1",
    "text.analyze@1",
    "calendar.public_holidays@1",  # M6: the first REAL integration: read-only, fixed https host, validated inputs, bounded (see tools/holidays.py)
})


def default_policy() -> ToolPolicy:
    return ToolPolicy(auto_run=AUTO_RUN)

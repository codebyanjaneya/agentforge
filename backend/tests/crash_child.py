"""Child process for the real-crash tests (M4-A1). NOT a test module.

Usage: python tests/crash_child.py <database-url> <user-id> <plan-id> <ttl> <heartbeat> [<owner-id>]

It is one real "application process": it starts the approved plan through the real ``PlanRunService`` (real fenced stores, real
lease heartbeat) against the given database with a stub agent that never finishes, prints ``RUN <run-id>`` and then waits to
be killed by the parent. Nothing here talks to a network or a provider."""

import asyncio
import sys
import uuid
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from agentplatform.execution import ExecutionService, Limits  # noqa: E402
from agentplatform.plan_runs import PlanRunService, RunRegistry  # noqa: E402
from agentplatform.providers import ProviderRegistry  # noqa: E402
from agentplatform.run_leases import LeaseConfig  # noqa: E402
from auth.principal import Principal  # noqa: E402
from authz.workspaces import authorize_personal_scope  # noqa: E402


class StuckOrchestrator:
    agent_names = ("ceo", "sales", "research", "marketing", "finance", "hiring", "support")

    async def run_agent(self, name, task, scope):
        print(f"CALL {name}", flush=True)
        await asyncio.sleep(3600)

    run_with_delegation = run_agent


async def main(url, user_id, plan_id, ttl, heartbeat, owner_id):
    engine = create_engine(url, connect_args={"timeout": 30, "check_same_thread": False} if url.startswith("sqlite") else {}, pool_pre_ping=True)
    sf = sessionmaker(bind=engine, expire_on_commit=False)
    principal = Principal(user_id=uuid.UUID(user_id), provider="google", provider_subject=f"s-{user_id}", email="child@example.com", is_active=True)
    scope = authorize_personal_scope(sf, principal)
    executions = ExecutionService(sf, orchestrator=StuckOrchestrator(), providers=ProviderRegistry({}), short_term=None, long_term=None, limits=Limits())
    registry = RunRegistry(owner_id=owner_id, config=LeaseConfig(ttl_seconds=ttl, heartbeat_seconds=heartbeat, sweep_seconds=1.0, shutdown_grace_seconds=1.0))
    service = PlanRunService(sf, executions=executions, registry=registry)
    run = await service.start(scope, uuid.UUID(plan_id))
    print(f"RUN {run.id}", flush=True)
    await asyncio.sleep(3600)


if __name__ == "__main__":
    url, user_id, plan_id, ttl, heartbeat = sys.argv[1:6]
    asyncio.run(main(url, user_id, plan_id, float(ttl), float(heartbeat), sys.argv[6] if len(sys.argv) > 6 else None))

"use client";

import Link from "next/link";
import { useState } from "react";
import { getApi } from "@/lib/backend";
import { nextAction } from "@/lib/studio/home";
import { PLAN_STATUS, relativeTime } from "@/lib/studio/view";
import { ApprovalRow } from "./_approvals";
import { ObjectiveComposer } from "./_composer";
import { ControlStrip, NextStep, StarterWorkforce, useStarterWorkforce } from "./_home";
import { Badge, EmptyState, ErrorNotice, PageHead, Section, Skeleton, useLoader } from "./_ui";

export default function StudioHome() {
  const agents = useLoader(() => getApi().listAgents("active"), []);
  const plans = useLoader(() => getApi().listPlans({ status: "active", limit: 5 }), []);
  const approvals = useLoader(() => getApi().listApprovals({ status: "pending", limit: 5 }), []);
  const demo = useLoader(() => getApi().getDemo(), []);
  const approvedPlan = plans.data?.items.find((x) => x.status === "approved");
  const runs = useLoader(() => getApi().listRuns(approvedPlan!.id), [approvedPlan?.id], !!approvedPlan);
  const customCount = agents.data?.items.length ?? 0;
  const starter = useStarterWorkforce(() => agents.reload());
  const [justAssembled, setJustAssembled] = useState(false); // keep showing what was created (and the exact tools) until the next visit
  const ready = !!agents.data && !!plans.data && !!approvals.data && (!approvedPlan || !!runs.data);
  const newest = runs.data?.items[0];
  const action = nextAction({
    agentCount: customCount,
    plans: plans.data?.items ?? [],
    pendingApprovals: approvals.data?.pending ?? 0,
    lastRun: approvedPlan ? (newest ? { id: newest.id, status: newest.status } : null) : undefined,
  });

  function here() {
    if (action.kind === "assemble") {
      setJustAssembled(true);
      void starter.add();
      return;
    }
    const target = document.getElementById("objective");
    target?.scrollIntoView({ behavior: "smooth", block: "center" });
    if (target instanceof HTMLElement) target.focus();
  }

  return (
    <div className="mx-auto max-w-5xl">
      <PageHead
        eyebrow="AgentForge Studio"
        title="A workforce you can hand real work to."
        lede="Specialist agents plan the work, use real tools to do it, and stop to ask you before anything sensitive. You approve the plan, then every exact action, and you can read everything that happened."
      />

      {ready ? (
        <div className="mt-6">
          <NextStep action={action} onHere={here} busy={starter.busy} />
        </div>
      ) : null}

      <div className="st-split mt-6">
        <div className="grid min-w-0 gap-6">
          <ObjectiveComposer examples={demo.data?.example_objective ? [demo.data.example_objective] : undefined} />
          <Section title="Where you stay in control">
            <ControlStrip />
          </Section>
          <Section title="Recent plans" aside={<Link href="/studio/plans">All plans →</Link>}>
            {plans.loading ? (
              <Skeleton lines={3} />
            ) : plans.error ? (
              <ErrorNotice error={plans.error} onRetry={plans.reload} />
            ) : plans.data!.items.length === 0 ? (
              <EmptyState title="No plans yet">Write an objective above and AgentForge will draft your first plan for review.</EmptyState>
            ) : (
              <div>
                {plans.data!.items.map((p) => (
                  <Link key={p.id} href={`/studio/plans/${p.id}`} className="st-row" style={{ gridTemplateColumns: "minmax(0,1fr) auto" }}>
                    <div className="min-w-0">
                      <div className="st-row-title truncate">{p.objective}</div>
                      <div className="st-muted text-xs">
                        {p.task_count} task{p.task_count === 1 ? "" : "s"} · revision {p.latest_revision} · {relativeTime(p.updated_at)}
                      </div>
                    </div>
                    <Badge tone={PLAN_STATUS[p.status].tone}>{PLAN_STATUS[p.status].short}</Badge>
                  </Link>
                ))}
              </div>
            )}
          </Section>
        </div>

        <aside className="grid min-w-0 content-start gap-6">
          {approvals.data && approvals.data.items.length ? (
            <Section title={`Waiting for you · ${approvals.data.pending}`} aside={<Link href="/studio/approvals">Open →</Link>}>
              <ul className="st-appr-list">
                {approvals.data.items.map((item) => (
                  <ApprovalRow key={item.id} item={item} />
                ))}
              </ul>
            </Section>
          ) : null}
          <Section title="Your workforce" aside={customCount ? <Link href="/studio/agents">Manage →</Link> : undefined}>
            {agents.loading ? (
              <Skeleton lines={2} height="2.4rem" />
            ) : agents.error ? (
              <ErrorNotice error={agents.error} onRetry={agents.reload} />
            ) : customCount === 0 || justAssembled ? (
              <StarterWorkforce state={{ ...starter, add: () => { setJustAssembled(true); return starter.add(); } }} />
            ) : (
              <div>
                {agents.data!.items.slice(0, 6).map((a) => (
                  <Link key={a.id} href={`/studio/agents/${a.id}`} className="st-row" style={{ gridTemplateColumns: "minmax(0,1fr) auto" }}>
                    <div className="min-w-0">
                      <div className="st-row-title truncate">{a.name}</div>
                      <div className="st-muted truncate text-xs">{a.role}</div>
                    </div>
                    <span className="st-chip st-mono">v{a.current_version}</span>
                  </Link>
                ))}
                <p className="st-muted mt-3 text-xs">
                  Built-in specialists are always available too. <Link href="/studio/templates">Add more from templates</Link>.
                </p>
              </div>
            )}
          </Section>
        </aside>
      </div>
    </div>
  );
}

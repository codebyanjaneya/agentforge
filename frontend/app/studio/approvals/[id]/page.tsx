"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { getApi } from "@/lib/backend";
import { phaseOf } from "@/lib/studio/approvals";
import { brief, riskLabel } from "@/lib/studio/tools";
import type { ToolRisk, ToolSideEffect } from "@/lib/studio/types";
import { relativeTime } from "@/lib/studio/view";
import { useEffect } from "react";
import { ApprovalStatusBadge, ArgumentTable, DecisionPanel, LoadFailure, useApproval } from "../../_approvals";
import { refreshInbox } from "../../_team";
import { InvocationLine } from "../../_tools";
import { BackLink, Skeleton, useLoader } from "../../_ui";

/** One request, read like a warrant: WHO is asking, WHAT exactly will run, WHY, the IMPACT, and the DECISION. Every fact is real backend data. */
export default function ApprovalDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { item, error, loading, setItem, refresh } = useApproval(id);
  const run = useLoader(() => getApi().getRun(item!.run_id!), [item?.run_id], !!item?.run_id && item.context?.started_by !== "another member"); // another member's run is not this person's to open: the request's own context is shown instead
  const plan = useLoader(() => getApi().getPlan(run.data!.plan_id), [run.data?.plan_id], !!run.data);
  // opening a request marks the caller's own notification about it read (the badge counts what still waits for a decision, so it is unaffected)
  useEffect(() => {
    let live = true;
    void getApi()
      .listNotifications({ unread: true, limit: 100 })
      .then(async (n) => {
        const mine = n.items.filter((x) => x.approval_id === id);
        for (const x of mine) await getApi().markNotificationRead(x.id);
        if (live && mine.length) refreshInbox();
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [id]);
  const call = useLoader(() => getApi().getToolInvocation(item!.invocation_id!), [item?.invocation_id], !!item?.invocation_id && item.context?.started_by !== "another member");

  if (loading && !item) return <div className="mx-auto max-w-4xl"><Skeleton lines={5} height="4rem" /></div>;
  if (!item) {
    return (
      <div className="mx-auto max-w-4xl">
        <BackLink href="/studio/approvals">Approvals</BackLink>
        <h1 className="sr-only">Approval request</h1>
        <div className="mt-4">{error ? <LoadFailure error={error} onRetry={refresh} /> : null}</div>
      </div>
    );
  }

  const task = run.data?.tasks.find((t) => t.id === item.task) ?? null;
  const planTask = plan.data?.revision.tasks.find((t) => t.id === item.task) ?? null;
  const meta = phaseOf(item);
  const risk = riskLabel(item.tool.risk as ToolRisk, item.tool.side_effect as ToolSideEffect);

  return (
    <div className="mx-auto max-w-4xl">
      <BackLink href="/studio/approvals">Approvals</BackLink>
      <header className="st-rise mt-3 flex flex-wrap items-end justify-between gap-4">
        <div className="min-w-0">
          <div className="st-eyebrow">Approval request · one exact action</div>
          <h1 className="st-h1 mt-1">{item.tool.title}</h1>
          <p className="st-muted m-0 mt-2 max-w-prose">{item.tool.summary}</p>
        </div>
        <ApprovalStatusBadge item={item} />
      </header>

      <article className="st-warrant" data-state={item.status} aria-label="The action to approve">
        <section className="st-warrant-row">
          <h2 className="st-warrant-label">Who</h2>
          <div>
            <div className="font-medium">{task?.agent.name ?? item.agent}</div>
            <div className="st-muted text-sm">
              {task ? (task.agent.source === "custom" && task.agent.version ? `Custom agent · exact version v${task.agent.version}` : "Built-in specialist") : "Agent"}
              {" · "}asking at step {item.seq} of its work
            </div>
          </div>
        </section>

        <section className="st-warrant-row">
          <h2 className="st-warrant-label">What</h2>
          <div className="min-w-0">
            <div>
              <span className="font-medium">{item.tool.title}</span> <span className="st-mono st-muted text-sm">{item.tool.ref}</span>
              <span className="st-muted text-sm"> · exactly this version</span>
            </div>
            <div className="st-muted mt-1 text-sm">With exactly these values (sensitive values are already redacted):</div>
            <ArgumentTable args={item.arguments} />
            <p className="st-muted st-mono m-0 mt-2 text-xs" title="The server recomputes this fingerprint from the action; a different tool, version, value, task or run would not match it.">
              Bound to this action only · fingerprint {item.payload_hash.slice(0, 12)}
            </p>
          </div>
        </section>

        <section className="st-warrant-row">
          <h2 className="st-warrant-label">Why</h2>
          <div className="min-w-0">
            {plan.data ? (
              <div className="text-sm">
                <span className="st-muted">Objective · </span>
                {plan.data.objective}
              </div>
            ) : item.context ? (
              <div className="text-sm">
                <span className="st-muted">Objective · </span>
                {item.context.objective}
                <span className="st-muted"> · run started by {item.context.started_by === "you" ? "you" : "another member of this workspace"}</span>
              </div>
            ) : null}
            {!task && item.context?.task_title ? (
              <div className="mt-1 text-sm">
                <span className="st-muted">Task · </span>
                {item.context.task_title}
              </div>
            ) : null}
            {task ? (
              <div className="mt-1 text-sm">
                <span className="st-muted">Task · </span>
                {task.title}
                {planTask ? <span className="st-muted"> — {planTask.description}</span> : null}
              </div>
            ) : null}
            {item.reason ? (
              <blockquote className="st-quote">
                {item.reason}
                <footer className="st-quote-note text-xs">In the agent’s own words. It is not verified and grants nothing.</footer>
              </blockquote>
            ) : (
              <p className="st-muted m-0 mt-1 text-sm">The agent gave no explanation for this step.</p>
            )}
          </div>
        </section>

        <section className="st-warrant-row">
          <h2 className="st-warrant-label">Impact</h2>
          <div className="min-w-0 text-sm">
            <div>{item.impact}</div>
            <div className="st-muted mt-1">
              Safety class: <strong>{risk.label}</strong>. Reversible: <strong>not declared</strong> — treat it as something that cannot be undone.
            </div>
            {item.tool.limitations.length ? (
              <ul className="st-cannot mt-2">
                {item.tool.limitations.map((limit) => (
                  <li key={limit}>{limit}</li>
                ))}
              </ul>
            ) : null}
          </div>
        </section>

        <section className="st-warrant-row">
          <h2 className="st-warrant-label">Decision</h2>
          <DecisionPanel item={item} onDecided={setItem} />
        </section>
      </article>

      {item.invocation_id ? (
        <section className="mt-8" aria-label="What happened after approval">
          <h2 className="st-h2">After approval</h2>
          <p className="st-muted m-0 mt-1 max-w-prose text-sm">{meta.explain}</p>
          <div className="mt-3">{call.data ? <ul className="st-calls"><InvocationLine item={{ ...call.data, input_preview: brief(call.data.input), output_preview: brief(call.data.output) }} showAgent showTask /></ul> : <Skeleton lines={1} height="3rem" />}</div>
        </section>
      ) : null}

      <p className="st-muted mt-8 text-sm">
        Requested {relativeTime(item.created_at)}.{" "}
        {item.run_id ? <Link href={`/studio/runs/${item.run_id}`}>Open the run →</Link> : null}
      </p>
    </div>
  );
}

"use client";

import type { PlanTask, RunDetail, RunProviderEvents, ToolInvocationItem } from "@/lib/studio/types";
import { PROVIDER_STATE, providerSentence, requiredActionStatus } from "@/lib/studio/resilience";
import { Badge } from "./_ui";

/** Provider retry states per task, from the events the backend recorded: waiting before a retry, retrying, recovered, rate limit exhausted, provider unavailable.
 *  Shown only when something happened; a live state is announced politely, a final one stays as history. */
export function ProviderStatus({ events, run }: { events: RunProviderEvents; run: RunDetail }) {
  const rows = run.tasks.filter((t) => events.by_task[t.id]).map((t) => ({ task: t, state: events.by_task[t.id] }));
  if (!rows.length) return null;
  const counts = (task: string) => events.items.filter((e) => e.task === task && e.kind === "waiting").length;
  return (
    <div role="status" aria-live="polite">
    <ul className="st-provider" aria-label="Model provider status per task">
      {rows.map(({ task, state }) => {
        const meta = PROVIDER_STATE[state.state];
        return (
          <li key={task.id} data-state={state.state}>
            <div className="flex flex-wrap items-center gap-2">
              <strong>{task.title}</strong>
              <Badge tone={meta.tone} pulse={meta.live && task.status === "running"}>
                {meta.label}
              </Badge>
              {counts(task.id) ? <span className="st-muted text-xs">{counts(task.id)} wait{counts(task.id) === 1 ? "" : "s"} so far</span> : null}
            </div>
            <p className="st-muted m-0 mt-1 text-sm">{providerSentence(state, task.status)}</p>
          </li>
        );
      })}
    </ul>
    </div>
  );
}

/** Tasks that REQUIRE a specific action: where it really stands. "Performed" appears only when the tool call is recorded as succeeded. */
export function RequiredActions({ tasks, run, calls, pendingTasks }: { tasks: PlanTask[]; run: RunDetail; calls: ToolInvocationItem[]; pendingTasks: Set<string> }) {
  const required = tasks.filter((t) => t.required_tool);
  if (!required.length) return null;
  return (
    <ul className="st-provider" aria-label="Required actions">
      {required.map((t) => {
        const status = run.tasks.find((r) => r.id === t.id)?.status ?? "pending";
        const s = requiredActionStatus(t.required_tool!, calls, t.id, pendingTasks.has(t.id), status);
        return (
          <li key={t.id}>
            <div className="flex flex-wrap items-center gap-2">
              <strong>{t.title}</strong>
              <span className="st-mono text-xs">{t.required_tool}</span>
              <Badge tone={s.tone}>{s.label}</Badge>
            </div>
            <p className="st-muted m-0 mt-1 text-sm">{s.detail}</p>
          </li>
        );
      })}
    </ul>
  );
}

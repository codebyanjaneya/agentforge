"use client";

// Plan rendering (M3-C): the execution rail. A plan is shown as numbered stages; everything inside one stage is
// independent (it may run at the same time), and every task names its agent, the pinned version and what it waits for.

import type { PlanTask, PlanWarning, Unsupported } from "@/lib/studio/types";
import { groupStages, warningLabel } from "@/lib/studio/view";
import { AgentChip, Badge, Notice } from "./_ui";

export function PlanStages({ tasks }: { tasks: PlanTask[] }) {
  const titles = new Map(tasks.map((t) => [t.id, t.title]));
  const stages = groupStages(tasks);
  return (
    <ol className="st-rail" aria-label="Plan stages in execution order">
      {stages.map((stage) => (
        <li key={stage.level} className="st-stage">
          <span className="st-node" aria-hidden="true">
            {stage.index}
          </span>
          <div className="st-stage-head">
            <span className="st-eyebrow">Stage {stage.index}</span>
            <span className="st-muted text-sm">{stage.parallel ? `${stage.tasks.length} independent tasks — can run at the same time` : "one task"}</span>
          </div>
          <ul className="m-0 list-none p-0">
            {stage.tasks.map((task) => (
              <li key={task.id} className="st-task">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="st-task-title">{task.title}</div>
                  <AgentChip agent={task.agent} />
                </div>
                <p className="mt-1.5 text-sm leading-relaxed" style={{ color: "var(--st-ink-2)" }}>
                  {task.description}
                </p>
                <p className="mt-1.5 text-sm">
                  <span className="st-eyebrow mr-2">Delivers</span>
                  {task.expected_output}
                </p>
                <div className="st-task-meta">
                  {task.depends_on.length ? (
                    <span className="st-chip">after: {task.depends_on.map((d) => titles.get(d) ?? d).join(", ")}</span>
                  ) : (
                    <span className="st-chip">starts immediately</span>
                  )}
                  <span className="st-chip">complexity: {task.complexity}</span>
                  <span className="st-chip">priority: {task.priority}</span>
                  {task.required_tool ? (
                    <span className="st-chip" title="The task is not complete until this tool was really invoked. A sensitive tool pauses for a person's approval first.">
                      required action: <span className="st-mono">{task.required_tool}</span>
                    </span>
                  ) : null}
                </div>
                {task.rationale || task.dependency_reason ? (
                  <details className="mt-2 text-sm" style={{ color: "var(--st-ink-3)" }}>
                    <summary className="cursor-pointer">Why this agent{task.dependency_reason ? " and this order" : ""}</summary>
                    <p className="mt-1">{task.rationale}</p>
                    {task.dependency_reason ? <p className="mt-1">{task.dependency_reason}</p> : null}
                  </details>
                ) : null}
              </li>
            ))}
          </ul>
        </li>
      ))}
    </ol>
  );
}

export function PlanFindings({ warnings, unsupported }: { warnings: PlanWarning[]; unsupported: Unsupported[] }) {
  if (!warnings.length && !unsupported.length) return null;
  return (
    <div className="grid gap-3">
      {unsupported.length ? (
        <Notice tone="warn" title="Parts of your objective AgentForge cannot do">
          <p className="mt-1 text-sm">Agents only produce written results. These requirements are not covered by this plan:</p>
          <ul>
            {unsupported.map((u, i) => (
              <li key={i}>
                <strong>{u.requirement}</strong> — {u.reason}
              </li>
            ))}
          </ul>
        </Notice>
      ) : null}
      {warnings.length ? (
        <Notice tone="warn" title="Worth a look before you approve">
          <ul>
            {warnings.map((w, i) => (
              <li key={i}>
                <Badge tone="warn">{warningLabel(w.code)}</Badge> {w.task ? <span className="st-mono">{w.task}: </span> : null}
                {w.message}
              </li>
            ))}
          </ul>
        </Notice>
      ) : null}
    </div>
  );
}

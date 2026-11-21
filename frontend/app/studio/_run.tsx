"use client";

// Run rendering (M3-C): the same rail as the plan, but every state shown is the backend's real task state. Outputs are only
// ever the stored execution results; nothing is estimated, animated into "progress" or assumed to have succeeded.

import { useState } from "react";
import { getApi } from "@/lib/backend";
import type { ApprovalItem, RunDetail, TaskRun, TaskRunDetail, ToolInvocationItem } from "@/lib/studio/types";
import { RUN_STATUS, TASK_STATUS, duration, explainError, explainTask, groupStages, type Explained } from "@/lib/studio/view";
import { AgentChip, Badge, ErrorNotice, Spinner } from "./_ui";
import { ToolTrail } from "./_tools";
import { TaskApprovals } from "./_approvals";

function TaskOutput({ runId, task }: { runId: string; task: TaskRun }) {
  const [detail, setDetail] = useState<TaskRunDetail | null>(null);
  const [error, setError] = useState<Explained | null>(null);
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState(false);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      setDetail(await getApi().getTaskRun(runId, task.id));
    } catch (e) {
      setError(explainError(e));
    } finally {
      setLoading(false);
    }
  }

  function toggle() {
    const next = !open;
    setOpen(next);
    // Loaded on demand (and again for a running task, whose record changes): a poll never fetches every task's output.
    if (next && (!detail || task.status === "running")) void load();
  }

  return (
    <div className="mt-2">
      <button className="st-btn st-btn-sm" onClick={toggle} aria-expanded={open}>
        {open ? "Hide details" : task.status === "succeeded" ? "View output" : "View details"}
      </button>
      {open ? (
        <div className="mt-2">
          {loading ? <Spinner label="Loading details" /> : null}
          {error ? <ErrorNotice error={error} onRetry={() => void load()} /> : null}
          {detail ? (
            <div className="grid gap-2">
              {detail.execution ? (
                <>
                  <div className="st-task-meta">
                    <span className="st-chip">
                      execution <span className="st-mono">{detail.execution.id.slice(0, 8)}</span>
                    </span>
                    {detail.execution.provider ? <span className="st-chip">{detail.execution.provider}{detail.execution.model ? ` · ${detail.execution.model}` : ""}</span> : null}
                    <span className="st-chip">{detail.execution.status}</span>
                  </div>
                  {detail.execution.result ? (
                    <>
                      <div className="st-eyebrow">Result</div>
                      <pre className="st-output" tabIndex={0}>{detail.execution.result}</pre>
                    </>
                  ) : (
                    <div className="st-muted text-sm">{detail.execution.status === "running" ? "No result yet — the agent is still working." : "This execution produced no result."}</div>
                  )}
                  <details>
                    <summary className="st-muted cursor-pointer text-sm">What the agent was asked</summary>
                    <pre className="st-output" tabIndex={0}>{detail.execution.input}</pre>
                  </details>
                </>
              ) : (
                <div className="st-muted text-sm">This task never started, so there is no execution record.</div>
              )}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

export function RunStages({ run, now, tools = {}, approvals = {} }: { run: RunDetail; now: number; tools?: Record<string, ToolInvocationItem[]>; approvals?: Record<string, ApprovalItem[]> }) {
  const stages = groupStages(run.tasks);
  return (
    <ol className="st-rail" aria-label="Task status by stage">
      {stages.map((stage) => {
        const states = stage.tasks.map((t) => t.status);
        const state = states.some((s) => s === "failed") ? "bad" : states.every((s) => s === "succeeded") ? "done" : states.some((s) => s === "running") ? "active" : "idle";
        return (
          <li key={stage.level} className="st-stage" data-state={state}>
            <span className="st-node" aria-hidden="true">
              {stage.index}
            </span>
            <div className="st-stage-head">
              <span className="st-eyebrow">Stage {stage.index}</span>
              <span className="st-muted text-sm">{stage.parallel ? `${stage.tasks.length} tasks in parallel` : "one task"}</span>
            </div>
            <ul className="m-0 list-none p-0">
              {stage.tasks.map((task) => {
                const meta = TASK_STATUS[task.status];
                const why = explainTask(task, run.tasks);
                const elapsed = duration(task.started_at, task.completed_at, now);
                return (
                  <li key={task.id} className="st-task" data-tone={meta.tone} id={`task-${task.id}`}>
                    <div className="flex flex-wrap items-start justify-between gap-2">
                      <div className="st-task-title">{task.title}</div>
                      <Badge tone={meta.tone} pulse={task.status === "running"}>
                        {task.status === "running" ? null : <span aria-hidden="true">{meta.glyph} </span>}{meta.label}
                      </Badge>
                    </div>
                    <div className="st-task-meta">
                      <AgentChip agent={task.agent} />
                      {elapsed ? <span className="st-chip st-mono">{elapsed}</span> : null}
                      {task.depends_on.length ? <span className="st-chip">after {task.depends_on.length} task{task.depends_on.length > 1 ? "s" : ""}</span> : null}
                      {task.reused ? <span className="st-chip" title="Carried over from an earlier attempt: not executed again">reused · attempt {task.reused.attempt}</span> : null}
                    </div>
                    {why ? (
                      <p className="mt-2 text-sm" style={{ color: task.status === "failed" ? "var(--st-bad)" : "var(--st-ink-2)" }}>
                        {why}
                      </p>
                    ) : null}
                    <TaskApprovals items={approvals[task.id] ?? []} />
                    <ToolTrail items={tools[task.id] ?? []} />
                    {task.execution_id ? <TaskOutput runId={run.id} task={task} /> : null}
                  </li>
                );
              })}
            </ul>
          </li>
        );
      })}
    </ol>
  );
}

export const runBadge = (status: RunDetail["status"]) => {
  const meta = RUN_STATUS[status];
  return (
    <Badge tone={meta.tone} pulse={status === "running"}>
      {meta.label}
    </Badge>
  );
};

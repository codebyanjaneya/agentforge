// Pure view-model helpers for the Studio (M3-C): status vocabulary, stage grouping, honest run summaries, plan lifecycle
// rules and error explanations. No React and no network, so every rule here is unit-tested.

import { ApiError } from "../api-client.ts";
import type { PlanDetail, PlanTask, RunDetail, RunStatus, TaskRun, TaskRunStatus } from "./types.ts";

export type Tone = "idle" | "active" | "good" | "bad" | "warn" | "muted";

export const RUN_STATUS: Record<RunStatus, { label: string; tone: Tone; terminal: boolean; blurb: string }> = {
  running: { label: "Running", tone: "active", terminal: false, blurb: "Agents are working through the plan." },
  succeeded: { label: "Completed", tone: "good", terminal: true, blurb: "Every task finished successfully." },
  partial: { label: "Partly completed", tone: "warn", terminal: true, blurb: "Some tasks finished; others failed or were skipped." },
  failed: { label: "Failed", tone: "bad", terminal: true, blurb: "No task finished successfully." },
  cancelled: { label: "Cancelled", tone: "muted", terminal: true, blurb: "The run was stopped before it finished." },
};

export const TASK_STATUS: Record<TaskRunStatus, { label: string; tone: Tone; glyph: string }> = {
  pending: { label: "Waiting", tone: "idle", glyph: "○" },
  running: { label: "Running", tone: "active", glyph: "◐" },
  succeeded: { label: "Succeeded", tone: "good", glyph: "●" },
  failed: { label: "Failed", tone: "bad", glyph: "✕" },
  skipped: { label: "Skipped", tone: "warn", glyph: "↷" },
  cancelled: { label: "Cancelled", tone: "muted", glyph: "■" },
};

export const PLAN_STATUS = {
  draft: { label: "Draft — awaiting your approval", short: "Draft", tone: "warn" as Tone },
  approved: { label: "Approved — ready to run", short: "Approved", tone: "good" as Tone },
  cancelled: { label: "Cancelled", short: "Cancelled", tone: "muted" as Tone },
};

export const isTerminalRun = (run: { status: RunStatus }): boolean => RUN_STATUS[run.status].terminal;

// ------------------------------------------------------------------------------------------------ stages

export type Stage<T> = { index: number; level: number; tasks: T[]; parallel: boolean };

/** Group tasks by dependency level: everything in one stage is independent and may run at the same time. */
export function groupStages<T extends { level: number; position: number }>(tasks: T[]): Stage<T>[] {
  const byLevel = new Map<number, T[]>();
  for (const t of [...tasks].sort((a, b) => a.level - b.level || a.position - b.position)) {
    byLevel.set(t.level, [...(byLevel.get(t.level) ?? []), t]);
  }
  return [...byLevel.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([level, list], index) => ({ index: index + 1, level, tasks: list, parallel: list.length > 1 }));
}

// ------------------------------------------------------------------------------------------------ run explanations

export type RunSummary = { headline: string; detail: string; tone: Tone; breakdown: { label: string; count: number; tone: Tone }[] };

export function summarizeRun(run: Pick<RunDetail, "status" | "counts">): RunSummary {
  const c = run.counts;
  const parts = (["succeeded", "running", "failed", "skipped", "cancelled", "pending"] as TaskRunStatus[])
    .filter((s) => c[s] > 0)
    .map((s) => ({ label: TASK_STATUS[s].label, count: c[s], tone: TASK_STATUS[s].tone }));
  const meta = RUN_STATUS[run.status];
  let headline = meta.label;
  if (run.status === "running") headline = `${c.succeeded} of ${c.total} tasks finished`;
  if (run.status === "partial") headline = `${c.succeeded} of ${c.total} tasks succeeded`;
  if (run.status === "succeeded") headline = `All ${c.total} tasks succeeded`;
  return { headline, detail: meta.blurb, tone: meta.tone, breakdown: parts };
}

/** Why a task ended up as it did, using only facts the backend returned. */
export function explainTask(task: TaskRun, all: TaskRun[]): string | null {
  const byId = new Map(all.map((t) => [t.id, t]));
  if (task.status === "skipped") {
    const blockers = task.depends_on.filter((d) => byId.get(d)?.status !== "succeeded").map((d) => byId.get(d)?.title ?? d);
    const why = task.error?.message ?? "A prerequisite did not succeed.";
    return blockers.length ? `${why} Blocked by: ${blockers.join(", ")}.` : why;
  }
  if (task.status === "failed" || task.status === "cancelled") return task.error?.message ?? null;
  if (task.status === "pending" && task.blocked_by.length) {
    return `Waiting for: ${task.blocked_by.map((d) => byId.get(d)?.title ?? d).join(", ")}.`;
  }
  return null;
}

/** The tasks a user must look at after a run ended: failed first, then skipped, then cancelled. */
export function attention(run: Pick<RunDetail, "tasks">): TaskRun[] {
  const rank: Record<string, number> = { failed: 0, skipped: 1, cancelled: 2 };
  return run.tasks.filter((t) => t.status in rank).sort((a, b) => rank[a.status] - rank[b.status] || a.position - b.position);
}

// ------------------------------------------------------------------------------------------------ plan lifecycle

/**
 * UX only, never security (the backend decides on every request): a plan or run is WRITTEN only by the member who created/started it. `owner` is that member's
 * e-mail as the server reports it (`created_by` / `started_by`). When either side is unknown (older rows, a session still loading) nothing is hidden and the server
 * answers; a mismatch hides the write actions. E-mails compare case-insensitively.
 */
export function mayWrite(me: string | null | undefined, owner: string | null | undefined): boolean {
  if (!me || !owner) return true;
  return me.trim().toLowerCase() === owner.trim().toLowerCase();
}

export type PlanActions = {
  canRevise: boolean;
  canValidate: boolean;
  canApprove: boolean;
  canStartRun: boolean;
  canCancel: boolean;
  startBlockedReason: string | null;
  /** False when somebody else created the plan: the person sees it but cannot change it. */
  canWrite: boolean;
};

/**
 * What the user may do. Approving never starts anything: starting is its own action and only exists for an APPROVED plan
 * whose approved revision has not already succeeded and has no run in progress (the backend enforces the same rules).
 */
export function planActions(plan: Pick<PlanDetail, "status" | "latest_run"> & { created_by?: string | null }, me?: string | null): PlanActions {
  const writer = mayWrite(me, plan.created_by);
  const open = plan.status !== "cancelled";
  const latest = plan.latest_run?.status;
  let reason: string | null = null;
  if (plan.status === "draft") reason = "Approve the plan first. Approval does not start any work.";
  else if (plan.status === "cancelled") reason = "This plan was cancelled.";
  else if (latest === "running") reason = "A run is already in progress.";
  else if (latest === "succeeded") reason = "This version already completed successfully. Revise the plan to run it again.";
  if (!writer && open) reason = `Only ${plan.created_by} can change or run this plan.`;
  return {
    canRevise: open && writer,
    canValidate: open,
    canApprove: plan.status === "draft" && writer,
    canStartRun: plan.status === "approved" && reason === null && writer,
    canCancel: open && writer,
    startBlockedReason: reason,
    canWrite: writer,
  };
}

export type RunActions = { canCancel: boolean; canRetry: boolean; canWrite: boolean };

/** Cancel and retry belong to the member who started the run (the server enforces it). */
export function runActions(run: Pick<RunDetail, "status" | "retry_options"> & { started_by?: string | null }, me?: string | null): RunActions {
  const writer = mayWrite(me, run.started_by);
  return { canCancel: run.status === "running" && writer, canRetry: Boolean(run.retry_options?.allowed) && writer, canWrite: writer };
}

export function warningLabel(code: string): string {
  const known: Record<string, string> = {
    possible_external_action: "May need a real-world action",
    newer_version_available: "Newer agent version available",
    dependency_unsatisfiable: "Depends on an unavailable task",
  };
  return known[code] ?? code.replace(/_/g, " ");
}

// ------------------------------------------------------------------------------------------------ errors

export type Explained = {
  title: string;
  message: string;
  /** Backend validation findings to list (plan errors, unsupported requirements, field messages). */
  items: string[];
  /** Field-level messages keyed by form field path (agent form). */
  fields: Record<string, string>;
  /** The user must sign in again. */
  needsSignIn: boolean;
  /** Reloading the item would resolve it (conflicts). */
  stale: boolean;
};

const BY_CODE: Record<string, [string, string]> = {
  slug_taken: ["That identifier is taken", "You already have an agent with this identifier. Choose another."],
  agent_archived: ["This agent is archived", "Unarchive it to edit or use it."],
  version_conflict: ["This agent changed elsewhere", "A newer version exists. Reload to see it, then apply your change again."],
  limit_reached: ["Limit reached", "You reached a safety limit for this item. Archive or remove something first."],
  invalid_definition: ["The agent definition is invalid", "Please review the fields."],
  version_not_found: ["That version does not exist", "It may not have been created."],
  not_found: ["Not found", "This item doesn't exist or isn't yours."],
  revision_not_found: ["That revision does not exist", "It may not have been created."],
  revision_conflict: ["The plan changed", "A newer revision exists. Reload the plan, then apply your change again."],
  plan_cancelled: ["This plan was cancelled", "Cancelled plans cannot be changed or run."],
  plan_not_approved: ["The plan isn't approved", "Only an approved plan can be run. Approval does not start work by itself."],
  plan_stale: ["The approval is out of date", "Approve the current revision, then start the run."],
  run_in_progress: ["A run is already in progress", "Open the running plan to follow it."],
  already_executed: ["This version already completed", "Revise the plan to run it again."],
  plan_invalid: ["The plan can no longer run as approved", "An agent assigned to a task is no longer available at its approved version."],
  plan_rejected: ["The plan didn't pass validation", "Nothing was saved."],
  objective_not_plannable: ["AgentForge can't plan this safely", "No available agent can do what this objective needs."],
  objective_invalid: ["The objective can't be used", "Please rewrite it."],
  invalid_cursor: ["The list changed", "Reload the list."],
  busy: ["AgentForge is busy", "Too many tasks are running right now. Try again in a moment."],
  timeout: ["The model took too long", "Nothing was saved. Try again, or make the objective more specific."],
  rate_limited: ["The model provider is rate-limiting", "Wait a moment and try again."],
  unavailable: ["The model provider is unavailable", "Try again shortly."],
  invalid_output: ["The planner's answer was unusable", "Nothing was saved. Try again or rephrase."],
  invalid_response: ["The model returned an unusable answer", "Nothing was saved. Try again."],
  unsupported_model: ["The agent's model isn't supported", "Edit the agent and choose a supported model."],
};

export function explainError(error: unknown, fallbackTitle = "That didn't work"): Explained {
  const out: Explained = { title: fallbackTitle, message: "Something went wrong. Please try again.", items: [], fields: {}, needsSignIn: false, stale: false };
  if (!(error instanceof ApiError)) return out;
  out.message = error.userMessage;
  out.needsSignIn = error.kind === "auth";
  out.stale = error.kind === "conflict";
  const d = error.detail;
  if (d?.fields?.length) {
    out.title = "Please fix the highlighted fields";
    out.message = "The server rejected some values.";
    for (const f of d.fields) {
      const path = f.path.replace(/^definition\./, "");
      out.fields[path] ??= f.message;
      out.items.push(`${path || "request"}: ${f.message}`);
    }
  }
  const known = d?.code ? BY_CODE[d.code] : undefined;
  if (known) [out.title, out.message] = known;
  else if (error.kind === "forbidden") out.title = "No access";
  else if (error.kind === "network") out.title = "Can't reach AgentForge";
  else if (error.kind === "rate_limited") out.title = "Slow down for a moment";
  else if (error.kind === "auth") out.title = "Signed out";
  if (d?.message && !known) out.message = d.message;
  if (d?.interpretation) out.items.push(`How AgentForge read it: ${d.interpretation}`);
  for (const u of d?.unsupported ?? []) out.items.push(`${u.requirement} — ${u.reason}`);
  for (const e of d?.errors ?? []) out.items.push(`${e.task ? `${e.task}: ` : ""}${e.message}`);
  if (d?.limit) out.message += ` (${d.limit})`;
  return out;
}

// ------------------------------------------------------------------------------------------------ time

/** The backend stores naive UTC timestamps without a zone suffix: read them as UTC. */
export function asUtc(iso: string): Date {
  return new Date(/[zZ]|[+-]\d{2}:?\d{2}$/.test(iso) ? iso : `${iso}Z`);
}

export function relativeTime(iso: string | null, now = Date.now()): string {
  if (!iso) return "—";
  const ms = now - asUtc(iso).getTime();
  if (Number.isNaN(ms)) return iso;
  const s = Math.round(ms / 1000);
  if (s < 45) return "just now";
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h} h ago`;
  const d = Math.round(h / 24);
  return d < 30 ? `${d} d ago` : asUtc(iso).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

export function duration(startIso: string | null, endIso: string | null, now = Date.now()): string | null {
  if (!startIso) return null;
  const end = endIso ? asUtc(endIso).getTime() : now;
  const sec = Math.max(0, Math.round((end - asUtc(startIso).getTime()) / 1000));
  if (sec < 60) return `${sec}s`;
  return `${Math.floor(sec / 60)}m ${String(sec % 60).padStart(2, "0")}s`;
}

export const agentLabel = (task: Pick<PlanTask, "agent">): string =>
  task.agent.source === "custom" && task.agent.version ? `${task.agent.name} · v${task.agent.version}` : task.agent.name;

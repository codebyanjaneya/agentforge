// Pure logic for the human-approval screens (M4-C). Nothing here talks to the network or decides anything: the SERVER decides every transition,
// recomputes every hash and enforces every rule. This file only says things plainly.

import type { ApprovalItem, ApprovalStatus } from "./types.ts";
import { asUtc } from "./view.ts";

type Tone = "good" | "bad" | "warn" | "muted" | "active";

/** What each state means, in words a person acts on. */
export const APPROVAL_STATUS: Record<ApprovalStatus, { label: string; tone: Tone; seal: string; explain: string }> = {
  pending: { label: "Waiting for you", tone: "active", seal: "PENDING", explain: "The agent is paused at this step. Nothing has happened yet, and nothing will until you decide." },
  approved: { label: "Approved", tone: "good", seal: "APPROVED", explain: "You authorised this exact action. It is about to run." },
  consumed: { label: "Approved and run", tone: "good", seal: "AUTHORISED", explain: "You authorised this exact action and it was dispatched once. This approval cannot be used again." },
  rejected: { label: "Rejected", tone: "bad", seal: "REJECTED", explain: "You rejected this action. It did not run, and the agent was told it was not approved." },
  expired: { label: "Expired", tone: "muted", seal: "EXPIRED", explain: "Nobody decided in time, so the action did not run. The agent may ask again." },
  invalidated: { label: "No longer valid", tone: "warn", seal: "VOID", explain: "This approval no longer matches a live action (the task stopped, was cancelled or restarted, or the action changed). It can no longer authorise anything." },
};

export const isOpen = (status: ApprovalStatus): boolean => status === "pending";

/** Fixed, user-safe text for the server's refusal codes: raw server text is never shown. */
const ERRORS: Record<string, string> = {
  approval_pending: "This approval is still waiting.",
  approval_approved: "This action was already approved.",
  approval_rejected: "This action was already rejected.",
  approval_expired: "This request expired before anyone decided, so it can no longer be approved.",
  approval_invalidated: "This request is no longer valid, so it can no longer be approved.",
  approval_consumed: "This approval was already used for the action it authorised.",
  approval_stale: "This request changed while you were looking at it. Reload it and review it again before deciding.",
  not_found: "This request does not exist, or it belongs to a different workspace.",
};
export function approvalError(code: string | undefined): string {
  return (code && ERRORS[code]) || "That decision could not be recorded. Reload the request and try again.";
}

/** Seconds until the request expires (0 when it has). */
export function secondsLeft(expiresAt: string, now = Date.now()): number {
  return Math.max(0, Math.floor((asUtc(expiresAt).getTime() - now) / 1000));
}

export function countdown(seconds: number): string {
  if (seconds <= 0) return "expired";
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m >= 60 ? `${Math.floor(m / 60)} h ${m % 60} min left` : `${m}:${String(s).padStart(2, "0")} left`;
}

/** The validated arguments as readable rows. Strings are shown as written; everything else as compact JSON. Nothing is hidden: the server already redacted secrets. */
export function argumentRows(args: Record<string, unknown> | null): { name: string; value: string; long: boolean }[] {
  if (!args) return [];
  return Object.entries(args).map(([name, value]) => {
    const text = typeof value === "string" ? value : JSON.stringify(value);
    return { name, value: text, long: text.length > 72 };
  });
}

/** One line for lists: "Send a note (simulated) → recipient: …". */
export function actionSummary(item: Pick<ApprovalItem, "tool" | "arguments">): string {
  const first = argumentRows(item.arguments)[0];
  const tail = first ? ` · ${first.name}: ${first.value.length > 40 ? `${first.value.slice(0, 40)}…` : first.value}` : "";
  return `${item.tool.title}${tail}`;
}

/** Pending first (oldest waiting first), then the history newest first. */
export function inboxOrder(items: ApprovalItem[]): { waiting: ApprovalItem[]; history: ApprovalItem[] } {
  const waiting = items.filter((i) => i.status === "pending").sort((a, b) => asUtc(a.created_at).getTime() - asUtc(b.created_at).getTime());
  const history = items.filter((i) => i.status !== "pending").sort((a, b) => asUtc(b.created_at).getTime() - asUtc(a.created_at).getTime());
  return { waiting, history };
}

/** Group a run's approvals by task key, newest first. */
export function approvalsByTask(items: ApprovalItem[]): Record<string, ApprovalItem[]> {
  const out: Record<string, ApprovalItem[]> = {};
  for (const item of items) if (item.task) (out[item.task] ??= []).push(item);
  for (const list of Object.values(out)) list.sort((a, b) => asUtc(b.created_at).getTime() - asUtc(a.created_at).getTime());
  return out;
}

// ------------------------------------------------------------------------------------------------ durable lifecycle (M4-C finalization)

export type Phase = { label: string; tone: Tone; seal: string; explain: string };

/** The state of a request as a person should read it: it distinguishes a task that is SUSPENDED (saved, held by no process) from a request that is only waiting, and an
 * approval that is being resumed from one that already ran. Derived from the real record; never invented. */
export function phaseOf(item: Pick<ApprovalItem, "status" | "suspended" | "task">): Phase {
  const base = APPROVAL_STATUS[item.status];
  if (item.status === "pending" && item.suspended) {
    return { label: "Suspended · waiting for you", tone: "active", seal: "SUSPENDED", explain: "The task is paused and saved in the database. No process is holding it, so it survives a restart. It resumes only after you decide, and only this exact call can run." };
  }
  if (item.status === "approved" && item.suspended) {
    return { label: "Approved · resuming", tone: "good", seal: "APPROVED", explain: "You authorised this exact action. The task is waiting for a process to pick it up and run it; this can take a few seconds." };
  }
  if (item.status === "consumed" && item.task) {
    return { label: "Approved · resumed and run", tone: "good", seal: "AUTHORISED", explain: "You authorised this exact action. The task resumed from where it was suspended and the action was dispatched once. This approval cannot be used again." };
  }
  return base;
}

/** Plain words for the approval and suspension events in a run's history. */
export const EVENT_TEXT: Record<string, { label: string; tone: Tone }> = {
  approval_requested: { label: "Approval requested", tone: "active" },
  task_suspended: { label: "Task suspended: waiting for a person", tone: "active" },
  approval_approved: { label: "Approved by a person", tone: "good" },
  approval_rejected: { label: "Rejected by a person", tone: "bad" },
  approval_expired: { label: "Approval expired: nobody decided in time", tone: "muted" },
  approval_invalidated: { label: "Approval no longer valid", tone: "warn" },
  approval_consumed: { label: "The approved action was dispatched, once", tone: "good" },
  task_resumed: { label: "Task resumed from its saved state", tone: "good" },
};
export const isLifecycleEvent = (type: string): boolean => type in EVENT_TEXT || type === "run_resumed" || type === "run_claimed" || type === "recovery_performed" || type === "task_interrupted";

const ACTOR: Record<string, string> = { user: "a person", api: "the server", worker: "a worker", recovery: "recovery" };
export function actorLabel(actor: string): string {
  return ACTOR[actor] ?? "the system";
}

/** One history line: the label plus the exact tool and step when the event carries them (never values: events do not contain any). */
export function eventDetail(detail: Record<string, string | number | boolean | string[]>): string | null {
  const parts: string[] = [];
  if (typeof detail.tool === "string") parts.push(detail.tool);
  if (typeof detail.seq === "number") parts.push(`step ${detail.seq}`);
  if (typeof detail.reason === "string") parts.push(detail.reason.replaceAll("_", " "));
  return parts.length ? parts.join(" · ") : null;
}

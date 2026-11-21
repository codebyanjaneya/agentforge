// What the Studio home tells a person to do next. Pure: it reads only what the backend really reported, and invents nothing (no counts, no progress, no activity).

import type { PlanSummary } from "./types.ts";

export type NextAction = {
  kind: "approve" | "assemble" | "review" | "start" | "follow" | "result" | "delegate";
  title: string;
  detail: string;
  href: string | null; // null: the action is on this page (the starter panel or the composer)
  cta: string;
};

export type HomeFacts = {
  agentCount: number;
  plans: Pick<PlanSummary, "id" | "objective" | "status">[];
  pendingApprovals: number;
  /** The newest run of the newest approved plan: null when it never ran, undefined when it is not known (the caller waits). */
  lastRun?: { id: string; status: string } | null;
};

const OUTCOME: Record<string, string> = { succeeded: "completed", partial: "partly completed", failed: "failed", cancelled: "cancelled" };

const brief = (text: string, n = 70): string => (text.length > n ? `${text.slice(0, n - 1)}…` : text);

/** The single most useful next step, in priority order: a decision only a person can make, then the next step toward a first result. */
export function nextAction(facts: HomeFacts): NextAction {
  if (facts.pendingApprovals > 0) {
    const n = facts.pendingApprovals;
    return { kind: "approve", title: n === 1 ? "An agent is waiting for your decision" : `${n} agents are waiting for your decision`, detail: "They stopped before a sensitive action. Nothing runs until you approve the exact action, or reject it.", href: "/studio/approvals", cta: n === 1 ? "Review the request" : "Review the requests" };
  }
  if (facts.agentCount === 0) {
    return { kind: "assemble", title: "Assemble your workforce", detail: "Start with three specialists that use real tools: a unit economics analyst, a launch planner and a launch communications lead.", href: null, cta: "Add the starter workforce" };
  }
  const draft = facts.plans.find((p) => p.status === "draft");
  if (draft) return { kind: "review", title: "A plan is waiting for your review", detail: `“${brief(draft.objective)}” — read the tasks and the agents assigned, then approve it.`, href: `/studio/plans/${draft.id}`, cta: "Review the plan" };
  const approved = facts.plans.find((p) => p.status === "approved");
  if (approved && facts.lastRun) {
    const run = facts.lastRun;
    if (run.status === "running") return { kind: "follow", title: "A run is in progress", detail: `“${brief(approved.objective)}” — follow every task as it happens.`, href: `/studio/runs/${run.id}`, cta: "Follow the run" };
    return { kind: "result", title: "Your last run finished", detail: `“${brief(approved.objective)}” — ${OUTCOME[run.status] ?? run.status}. Read the result and everything that happened.`, href: `/studio/runs/${run.id}`, cta: "See the result" };
  }
  if (approved) return { kind: "start", title: "An approved plan is ready to run", detail: `“${brief(approved.objective)}” — start it when you are ready.`, href: `/studio/plans/${approved.id}`, cta: "Open the plan" };
  return { kind: "delegate", title: "Delegate your next objective", detail: "Describe the outcome you want. You will see the plan before anything runs.", href: null, cta: "Write an objective" };
}

/** Where the work is controlled, said plainly: what runs by itself and what asks first. */
export const CONTROL_LINES: { name: string; text: string }[] = [
  { name: "Runs by itself", text: "Read-only tools that only compute an answer: calculator, unit economics, dates, text and tables. They change nothing." },
  { name: "Asks you first", text: "Anything that could act outside AgentForge. The agent pauses, you see the exact tool, version and values, and you approve that one call or reject it." },
  { name: "Always recorded", text: "Every task, tool call, approval and recovery is written to a history you can read afterwards. Nothing in it contains a secret value." },
];

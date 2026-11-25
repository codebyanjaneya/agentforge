// The Studio's first-run checklist. Pure: it reads only what the backend really reported (agents, plans, the newest run) and invents nothing: no progress bar, no
// percentage, no activity. A step is "done" because something exists, never because someone clicked past it.

import type { PlanSummary } from "./types.ts";

export type SetupStepId = "workforce" | "objective" | "approve" | "follow";
export type SetupStep = { id: SetupStepId; title: string; detail: string; state: "done" | "current" | "todo" };

export type SetupFacts = {
  agentCount: number;
  plans: Pick<PlanSummary, "id" | "status">[];
  /** The newest run of the newest approved plan: null when none ran. */
  lastRun: { id: string; status: string } | null;
};

const COPY: Record<SetupStepId, { title: string; detail: string }> = {
  workforce: {
    title: "Assemble your workforce",
    detail: "An agent is a specialist: written instructions, the tools it is allowed to use, and a model. Start with three ready-made specialists or create your own.",
  },
  objective: {
    title: "Delegate an objective",
    detail: "Describe the outcome you want. AgentForge drafts a plan with the tasks and the agent for each one. Nothing runs yet.",
  },
  approve: {
    title: "Approve the plan and start the run",
    detail: "Read each task and the agent assigned, then approve. Starting the run is a separate, explicit step.",
  },
  follow: {
    title: "Follow the run and read what happened",
    detail: "Watch tasks finish. When an agent needs to do something sensitive it stops and asks you, with the exact values.",
  },
};

const ORDER: SetupStepId[] = ["workforce", "objective", "approve", "follow"];

export function setupSteps(facts: SetupFacts): { steps: SetupStep[]; done: number; complete: boolean } {
  const ran = facts.lastRun !== null;
  const doneFlags: Record<SetupStepId, boolean> = {
    workforce: facts.agentCount > 0,
    objective: facts.plans.length > 0 || ran,
    approve: facts.plans.some((p) => p.status === "approved") || ran,
    follow: ran && facts.lastRun!.status !== "running",
  };
  const firstOpen = ORDER.find((id) => !doneFlags[id]);
  const steps = ORDER.map<SetupStep>((id) => ({ id, ...COPY[id], state: doneFlags[id] ? "done" : id === firstOpen ? "current" : "todo" }));
  const done = steps.filter((s) => s.state === "done").length;
  return { steps, done, complete: done === ORDER.length };
}

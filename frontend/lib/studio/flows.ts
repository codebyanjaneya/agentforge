// The user-facing workflows (M3-C), kept apart from React so their guarantees are testable: every step calls exactly the
// backend operation it names and nothing more. In particular, creating, revising or approving a plan NEVER starts a run:
// `startRun` is reachable only through `startRunFlow`, which refuses anything but an approved plan.

import type { ApiClient } from "../api-client.ts";
import { definitionFromForm, type AgentForm, type EditorTask, payloadFromEditor, type Preserved } from "./forms.ts";
import type { AgentDetail, PlanDetail, RunDetail } from "./types.ts";

export type FlowApi = Pick<
  ApiClient,
  "createPlan" | "revisePlan" | "approvePlan" | "validatePlan" | "cancelPlan" | "startRun" | "updateAgent" | "createAgent" | "rollbackAgent" | "instantiateTemplate"
>;

export class FlowRefused extends Error {
  constructor(message: string) {
    super(message);
    this.name = "FlowRefused";
  }
}

export const createPlanFlow = async (api: FlowApi, objective: string): Promise<PlanDetail> => {
  const text = objective.trim();
  if (text.length < 10 || text.length > 2000) throw new FlowRefused("The objective must be 10–2000 characters.");
  return api.createPlan(text); // planning only
};

export const revisePlanFlow = (api: FlowApi, plan: Pick<PlanDetail, "id" | "latest_revision">, tasks: EditorTask[], interpretation: string | null) =>
  api.revisePlan(plan.id, payloadFromEditor(tasks, plan.latest_revision, interpretation)); // a revision withdraws approval; never runs

export const approvePlanFlow = async (api: FlowApi, plan: Pick<PlanDetail, "id" | "status" | "latest_revision">): Promise<PlanDetail> => {
  if (plan.status !== "draft") throw new FlowRefused("Only a draft plan can be approved.");
  return api.approvePlan(plan.id, plan.latest_revision); // approval only: no run is started
};

export const startRunFlow = async (api: FlowApi, plan: Pick<PlanDetail, "id" | "status">): Promise<RunDetail> => {
  if (plan.status !== "approved") throw new FlowRefused("Only an approved plan can be run.");
  return api.startRun(plan.id);
};

export const saveAgentFlow = (api: FlowApi, agent: Pick<AgentDetail, "id" | "latest_version" | "definition">, form: AgentForm, note: string) => {
  const preserved: Preserved = { metadata: agent.definition.metadata };
  return api.updateAgent(agent.id, { base_version: agent.latest_version, definition: definitionFromForm(form, preserved), ...(note.trim() ? { change_note: note.trim() } : {}) });
};

export const createAgentFlow = (api: FlowApi, slug: string, form: AgentForm, note: string) =>
  api.createAgent({ slug, definition: definitionFromForm(form), ...(note.trim() ? { change_note: note.trim() } : {}) });

/** Rollback creates a NEW version from an old one (history is never rewritten); the base is the version the user is looking at. */
export const rollbackFlow = (api: FlowApi, agent: Pick<AgentDetail, "id" | "latest_version">, toVersion: number) =>
  api.rollbackAgent(agent.id, { to_version: toVersion, base_version: agent.latest_version });

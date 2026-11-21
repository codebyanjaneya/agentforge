// Form models for the Agent Studio and the plan editor (M3-C). Client-side checks only mirror the backend's rules to give
// instant feedback; the backend stays the authority and its field errors are shown as returned. No ownership, workspace or
// version identifiers are ever part of a form: those come from the verified session on the server.

import type { AgentDefinition, Complexity, PlanRevision, Priority, RevisedTaskInput } from "./types.ts";

export const DEFAULT_MODEL = "llama-3.3-70b-versatile";
export const PROVIDERS = ["groq"] as const;
const SLUG = /^[a-z][a-z0-9-]{1,38}[a-z0-9]$/;
const RESERVED = new Set(["ceo", "sales", "marketing", "support", "research", "finance", "hiring", "agent", "agents", "new", "default", "system", "admin", "template", "templates", "me", "api"]);
const LABEL = /^[a-z][a-z0-9 _-]{1,39}$/;
const MODEL_NAME = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$/;
const TASK_ID = /^[a-z][a-z0-9_-]{0,31}$/;

export const BUILTIN_AGENTS: { key: string; name: string; blurb: string }[] = [
  { key: "ceo", name: "CEO", blurb: "Strategy, prioritisation and cross-team synthesis" },
  { key: "sales", name: "Sales", blurb: "Leads, outreach and pipeline" },
  { key: "marketing", name: "Marketing", blurb: "Campaigns, positioning and content" },
  { key: "support", name: "Support", blurb: "Customer replies and issue triage" },
  { key: "research", name: "Research", blurb: "Market, competitor and topic research" },
  { key: "finance", name: "Finance", blurb: "Budgets, forecasts and unit economics" },
  { key: "hiring", name: "Hiring", blurb: "Roles, sourcing and interview plans" },
];

// ------------------------------------------------------------------------------------------------ agent form

export type AgentForm = {
  name: string;
  description: string;
  role: string;
  purpose: string;
  instructions: string;
  capabilities: string; // comma separated
  input_expectations: string;
  output_expectations: string;
  model_name: string;
  temperature: string;
  max_tokens: string;
  /** M4-B: the tools this agent may use, as tool ids or pinned "id@version" references (the backend pins the exact versions on save). */
  tools: string[];
};

/** The parts of a stored definition the form does not edit: they are sent back unchanged. */
export type Preserved = Pick<AgentDefinition, "metadata">;

export const blankForm = (): AgentForm => ({
  name: "", description: "", role: "", purpose: "", instructions: "", capabilities: "", input_expectations: "", output_expectations: "",
  model_name: DEFAULT_MODEL, temperature: "0.7", max_tokens: "", tools: [],
});

export const formFromDefinition = (d: AgentDefinition): AgentForm => ({
  name: d.name, description: d.description, role: d.role, purpose: d.purpose, instructions: d.instructions, capabilities: d.capabilities.join(", "),
  input_expectations: d.input_expectations, output_expectations: d.output_expectations, model_name: d.model.name, temperature: String(d.model.temperature),
  max_tokens: d.model.max_tokens === null ? "" : String(d.model.max_tokens), tools: [...d.allowed_tools],
});

export const parseCapabilities = (raw: string): string[] => raw.split(",").map((c) => c.trim().toLowerCase()).filter(Boolean);

/** Build the request definition. Tools are chosen from the registry only (ids the backend knows); the backend re-validates and pins every one. */
export function definitionFromForm(form: AgentForm, preserved: Preserved = { metadata: {} }): AgentDefinition {
  const max = form.max_tokens.trim();
  return {
    name: form.name.trim(), description: form.description.trim(), role: form.role.trim(), purpose: form.purpose.trim(), instructions: form.instructions.trim(),
    capabilities: parseCapabilities(form.capabilities), allowed_tools: [...form.tools], input_expectations: form.input_expectations.trim(),
    output_expectations: form.output_expectations.trim(),
    model: { provider: "groq", name: form.model_name.trim(), temperature: Number(form.temperature), max_tokens: max === "" ? null : Number(max) },
    metadata: preserved.metadata,
  };
}

const between = (value: string, min: number, max: number, label: string): string | null => {
  const n = value.trim().length;
  return n < min || n > max ? `${label} must be ${min}–${max} characters` : null;
};

export function validateForm(form: AgentForm): Record<string, string> {
  const errors: Record<string, string> = {};
  const set = (key: string, message: string | null) => {
    if (message) errors[key] = message;
  };
  set("name", between(form.name, 2, 80, "Name"));
  set("description", between(form.description, 10, 500, "Description"));
  set("role", between(form.role, 2, 120, "Role"));
  set("purpose", between(form.purpose, 10, 500, "Purpose"));
  set("instructions", between(form.instructions, 20, 8000, "Instructions"));
  if (form.input_expectations.trim().length > 1000) errors.input_expectations = "At most 1000 characters";
  if (form.output_expectations.trim().length > 1000) errors.output_expectations = "At most 1000 characters";
  const caps = parseCapabilities(form.capabilities);
  if (caps.length > 12) errors.capabilities = "At most 12 capabilities";
  else if (caps.some((c) => !LABEL.test(c))) errors.capabilities = "Each capability is a short label: letters, digits, spaces, hyphens";
  else if (new Set(caps).size !== caps.length) errors.capabilities = "Capabilities must be unique";
  if (!MODEL_NAME.test(form.model_name.trim())) errors["model.name"] = "Enter a valid model name";
  const t = Number(form.temperature);
  if (form.temperature.trim() === "" || !Number.isFinite(t) || t < 0 || t > 2) errors["model.temperature"] = "Between 0 and 2";
  if (form.max_tokens.trim() !== "") {
    const m = Number(form.max_tokens);
    if (!Number.isInteger(m) || m < 1 || m > 8192) errors["model.max_tokens"] = "A whole number from 1 to 8192, or leave empty";
  }
  return errors;
}

export function validateSlug(slug: string): string | null {
  if (!SLUG.test(slug)) return "3–40 characters: lowercase letters, digits and hyphens, starting with a letter";
  if (slug.includes("--")) return "No consecutive hyphens";
  if (RESERVED.has(slug)) return "That identifier is reserved";
  return null;
}

/** A readable identifier suggestion from a name (the user can change it). */
export function suggestSlug(name: string): string {
  const base = name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").replace(/-+/g, "-").slice(0, 40).replace(/-+$/g, "");
  return /^[a-z]/.test(base) ? base : base ? `agent-${base}`.slice(0, 40) : "";
}

// ------------------------------------------------------------------------------------------------ plan editor

export type EditorTask = {
  id: string;
  title: string;
  description: string;
  expected_output: string;
  agent: string;
  depends_on: string[];
  dependency_reason: string;
  complexity: Complexity;
  priority: Priority;
  rationale: string;
  required_tool: string | null; // carried through a revision unchanged: editing a task must never silently drop its required action
  isNew: boolean;
};

export const editorFromRevision = (rev: Pick<PlanRevision, "tasks">): EditorTask[] =>
  rev.tasks.map((t) => ({
    id: t.id, title: t.title, description: t.description, expected_output: t.expected_output, agent: t.agent.key, depends_on: [...t.depends_on],
    dependency_reason: t.dependency_reason ?? "", complexity: t.complexity, priority: t.priority, rationale: t.rationale, required_tool: t.required_tool ?? null, isNew: false,
  }));

export function nextTaskId(tasks: { id: string }[]): string {
  const used = new Set(tasks.map((t) => t.id));
  for (let n = tasks.length + 1; n < 1000; n++) if (!used.has(`task-${n}`)) return `task-${n}`;
  return `task-${Date.now() % 100000}`;
}

export const blankTask = (tasks: { id: string }[], agent = "research"): EditorTask => ({
  id: nextTaskId(tasks), title: "", description: "", expected_output: "", agent, depends_on: [], dependency_reason: "", complexity: "medium", priority: "normal", rationale: "", required_tool: null, isNew: true,
});

/** Removing a task also removes it from every dependency list. */
export const removeTask = (tasks: EditorTask[], id: string): EditorTask[] =>
  tasks.filter((t) => t.id !== id).map((t) => ({ ...t, depends_on: t.depends_on.filter((d) => d !== id) }));

export function validateEditor(tasks: EditorTask[]): Record<string, string> {
  const errors: Record<string, string> = {};
  if (tasks.length < 1) errors._plan = "A plan needs at least one task";
  if (tasks.length > 12) errors._plan = "A plan has at most 12 tasks";
  const ids = new Set<string>();
  for (const t of tasks) {
    const key = (field: string) => `${t.id}.${field}`;
    if (!TASK_ID.test(t.id)) errors[key("id")] = "Invalid task id";
    if (ids.has(t.id)) errors[key("id")] = "Duplicate task id";
    ids.add(t.id);
    if (t.title.trim().length < 3 || t.title.trim().length > 120) errors[key("title")] = "Title must be 3–120 characters";
    if (t.description.trim().length < 10 || t.description.trim().length > 1500) errors[key("description")] = "Description must be 10–1500 characters";
    if (t.expected_output.trim().length < 5 || t.expected_output.trim().length > 600) errors[key("expected_output")] = "Expected output must be 5–600 characters";
    if (!t.agent.trim()) errors[key("agent")] = "Choose an agent";
    if (t.depends_on.includes(t.id)) errors[key("depends_on")] = "A task cannot depend on itself";
  }
  for (const t of tasks) if (t.depends_on.some((d) => !ids.has(d))) errors[`${t.id}.depends_on`] = "Depends on a task that doesn't exist";
  return errors;
}

/** The revision request. It carries the revision the user was looking at (the backend refuses a stale base). */
export function payloadFromEditor(tasks: EditorTask[], baseRevision: number, interpretation: string | null): {
  base_revision: number;
  interpretation?: string | null;
  tasks: RevisedTaskInput[];
} {
  return {
    base_revision: baseRevision,
    ...(interpretation && interpretation.trim().length >= 10 ? { interpretation: interpretation.trim() } : {}),
    tasks: tasks.map((t) => ({
      id: t.id, title: t.title.trim(), description: t.description.trim(), expected_output: t.expected_output.trim(), agent: t.agent.trim(), depends_on: t.depends_on,
      dependency_reason: t.dependency_reason.trim() || null, complexity: t.complexity, priority: t.priority,
      ...(t.rationale.trim().length >= 5 ? { rationale: t.rationale.trim() } : {}),
      ...(t.required_tool ? { required_tool: t.required_tool } : {}),
    })),
  };
}

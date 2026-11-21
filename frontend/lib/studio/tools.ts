// Presentation logic for tools (M4-B): turns the backend's JSON Schemas, statuses and errors into the words and shapes a person reads.
// Pure functions only (no React, no network), so every rule here is unit-tested without a browser. Nothing is invented: every label is derived
// from a field the backend sent.

import type { InvocationStatus, JsonSchema, SafeError, ToolInvocationItem, ToolRisk, ToolSideEffect } from "./types.ts";
import type { Tone } from "./view.ts";

type Schema = {
  type?: string;
  title?: string;
  description?: string;
  properties?: Record<string, Schema>;
  required?: string[];
  items?: Schema;
  anyOf?: Schema[];
  enum?: unknown[];
  $ref?: string;
  $defs?: Record<string, Schema>;
  default?: unknown;
  format?: string;
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  exclusiveMaximum?: number;
  minLength?: number;
  maxLength?: number;
  maxItems?: number;
};

export type Port = {
  name: string;
  type: string;
  required: boolean;
  description: string | null;
  constraints: string[];
  /** For a list of objects: the fields of one element. */
  fields: Port[];
};

const MAX_DEPTH = 3;

function resolve(schema: Schema, defs: Record<string, Schema>): Schema {
  if (schema.$ref) return defs[schema.$ref.split("/").pop() ?? ""] ?? {};
  return schema;
}

function variants(schema: Schema, defs: Record<string, Schema>): { options: Schema[]; nullable: boolean } {
  const resolved = resolve(schema, defs);
  if (!resolved.anyOf) return { options: [resolved], nullable: false };
  const options = resolved.anyOf.map((s) => resolve(s, defs));
  return { options: options.filter((s) => s.type !== "null"), nullable: options.some((s) => s.type === "null") };
}

/** A plain-language type: "number", "text", "date", "whole number", "list of Column summary", "one of: a | b". */
export function typeLabel(schema: Schema, defs: Record<string, Schema>): string {
  const { options } = variants(schema, defs);
  const labels = options.map((s) => {
    if (s.enum) return `one of: ${s.enum.map(String).join(" | ")}`;
    switch (s.type) {
      case "string":
        return s.format === "date" ? "date (YYYY-MM-DD)" : "text";
      case "integer":
        return "whole number";
      case "number":
        return "number";
      case "boolean":
        return "yes / no";
      case "array":
        return `list of ${s.items ? typeLabel(s.items, defs) : "values"}`;
      case "object":
        return s.title ? s.title.toLowerCase() : "object";
      default:
        return s.title ? s.title.toLowerCase() : "value";
    }
  });
  return labels.length ? labels.join(" or ") : "value";
}

/** Human-readable limits: "1–200 characters", "greater than 0", "at most 100", "default 8". */
export function constraintsOf(schema: Schema, defs: Record<string, Schema>): string[] {
  const { options } = variants(schema, defs);
  const s = options[0] ?? {};
  const out: string[] = [];
  if (s.minLength !== undefined || s.maxLength !== undefined) {
    out.push(s.minLength !== undefined && s.maxLength !== undefined ? `${s.minLength}–${s.maxLength.toLocaleString("en-US")} characters` : s.maxLength !== undefined ? `up to ${s.maxLength.toLocaleString("en-US")} characters` : `at least ${s.minLength} characters`);
  }
  if (s.exclusiveMinimum !== undefined) out.push(`greater than ${s.exclusiveMinimum}`);
  if (s.minimum !== undefined) out.push(`at least ${s.minimum}`);
  if (s.exclusiveMaximum !== undefined) out.push(`less than ${s.exclusiveMaximum}`);
  if (s.maximum !== undefined) out.push(`at most ${s.maximum.toLocaleString("en-US")}`);
  if (s.maxItems !== undefined) out.push(`at most ${s.maxItems} items`);
  const fallback = (schema as Schema).default;
  if (fallback !== undefined && fallback !== null) out.push(`default ${String(fallback)}`);
  return out;
}

/** The fields of a tool's input or output schema as ports: name, type, whether it is required, its description and limits. */
export function portsOf(schema: JsonSchema | null | undefined, depth = 0, inherited?: Record<string, Schema>): Port[] {
  const root = (schema ?? {}) as Schema;
  const defs = { ...(inherited ?? {}), ...(root.$defs ?? {}) };
  const properties = resolve(root, defs).properties ?? {};
  const required = new Set(resolve(root, defs).required ?? []);
  return Object.entries(properties).map(([name, prop]) => {
    const { options, nullable } = variants(prop, defs);
    const element = options[0]?.type === "array" && options[0].items ? resolve(options[0].items, defs) : null;
    return {
      name,
      type: typeLabel(prop, defs),
      required: required.has(name) && !nullable,
      description: resolve(prop, defs).description ?? options[0]?.description ?? null,
      constraints: constraintsOf(prop, defs),
      fields: element?.properties && depth < MAX_DEPTH ? portsOf(element as JsonSchema, depth + 1, defs) : [],
    };
  });
}

// ------------------------------------------------------------------------------------------------ statuses and risk

export const INVOCATION_STATUS: Record<InvocationStatus, { label: string; tone: Tone; glyph: string; explain: string }> = {
  running: { label: "Running", tone: "active", glyph: "◐", explain: "The tool is running now." },
  succeeded: { label: "Succeeded", tone: "good", glyph: "●", explain: "The tool ran and returned a result." },
  failed: { label: "Failed", tone: "bad", glyph: "✕", explain: "The tool could not complete the request." },
  timed_out: { label: "Timed out", tone: "bad", glyph: "◔", explain: "The tool did not finish within its time limit. AgentForge stopped waiting and used no result." },
  rejected: { label: "Refused", tone: "warn", glyph: "⊘", explain: "The request was refused before anything ran." },
  cancelled: { label: "Cancelled", tone: "muted", glyph: "■", explain: "The call was cancelled before it finished." },
  interrupted: { label: "Interrupted", tone: "warn", glyph: "↯", explain: "The server stopped before the call finished; no result was recorded." },
  uncertain: { label: "Outcome unknown", tone: "warn", glyph: "?", explain: "Whether this action took effect is unknown. It will not be sent again automatically: it needs to be reconciled first." },
};

const ERROR_TEXT: Record<string, string> = {
  operation_in_flight: "An earlier attempt at this exact action may still be in progress, so it was not sent again.",
  outcome_unknown: "An earlier attempt at this action has an unknown outcome. Reconcile it first: it was not sent again.",
  retry_not_permitted: "This action failed earlier and its tool does not allow an automatic retry, so it was not sent again.",
  not_dispatched: "The server stopped before this action was sent, so it was not performed.",
  not_permitted: "The agent asked for a tool it has not been given. Open the agent and add the tool to allow it.",
  unknown_tool: "The agent asked for a tool that does not exist.",
  invalid_input: "The agent's arguments did not fit the tool's input fields, so nothing ran.",
  tool_unavailable: "That version of the tool is switched off.",
  approval_required: "This kind of tool needs a human approval, which is not available here, so it was not run.",
  approval_rejected: "A person rejected this action, so it did not run.",
  approval_expired: "Nobody approved this action in time, so it did not run.",
  approval_invalid: "The approval no longer matched this exact action, so it did not run.",
  timeout: "The tool did not finish within its time limit; AgentForge stopped waiting and used no result.",
  tool_error: "The tool failed while running.",
  output_invalid: "The tool returned something that did not match its output fields.",
  output_too_large: "The tool's result was too large and was discarded.",
  limit_exceeded: "The agent reached its limit of tool calls for this task.",
  duplicate_call: "This exact step had already been dispatched.",
};

/** One sentence explaining why a call did not succeed (fixed backend codes only: never raw error text). */
export function explainInvocation(status: InvocationStatus, error: SafeError | null): string | null {
  if (status === "succeeded" || status === "running") return null;
  if (error && ERROR_TEXT[error.code]) return ERROR_TEXT[error.code];
  return INVOCATION_STATUS[status].explain;
}

export function riskLabel(risk: ToolRisk, sideEffect: ToolSideEffect): { label: string; detail: string; tone: Tone } {
  if (risk === "read" && sideEffect === "none") return { label: "Read-only", detail: "Computes an answer. Changes nothing inside or outside AgentForge.", tone: "good" };
  if (risk === "write") return { label: "Writes data", detail: "Can change data. Needs a human approval before it runs.", tone: "warn" };
  return { label: "External action", detail: "Acts outside AgentForge. Needs a human approval before it runs.", tone: "warn" };
}

// ------------------------------------------------------------------------------------------------ values and selection

/** A value as a short readable string for previews (objects become "key: value" pairs). */
export function brief(value: unknown, limit = 90): string {
  const text =
    value === null || value === undefined
      ? "—"
      : typeof value === "string"
        ? value
        : typeof value === "object"
          ? Object.entries(value as Record<string, unknown>)
              .map(([k, v]) => `${k}: ${typeof v === "object" && v !== null ? "…" : String(v)}`)
              .join(" · ")
          : String(value);
  return text.length <= limit ? text : `${text.slice(0, limit - 1)}…`;
}

export const durationLabel = (ms: number | null): string => (ms === null ? "" : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)} s`);

/** Group a run's invocations by the task that made them, in the order they were made. */
export function byTask(items: ToolInvocationItem[]): Record<string, ToolInvocationItem[]> {
  const out: Record<string, ToolInvocationItem[]> = {};
  for (const item of [...items].sort((a, b) => a.seq - b.seq || a.created_at.localeCompare(b.created_at))) {
    if (item.task) (out[item.task] ??= []).push(item);
  }
  return out;
}

/** Tool selection in the agent editor. Entries are tool ids or pinned "id@version" references; a tool counts as selected by id. */
export const isSelected = (selected: string[], toolId: string): boolean => selected.some((entry) => entry === toolId || entry.startsWith(`${toolId}@`));

export function toggleTool(selected: string[], toolId: string): string[] {
  return isSelected(selected, toolId) ? selected.filter((entry) => !(entry === toolId || entry.startsWith(`${toolId}@`))) : [...selected, toolId];
}

export const MAX_TOOLS_PER_AGENT = 8;

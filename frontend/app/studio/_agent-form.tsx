"use client";

import type { AgentForm } from "@/lib/studio/forms";
import { Field } from "./_ui";
import { ToolPicker } from "./_tools";

type Props = { form: AgentForm; onChange: (patch: Partial<AgentForm>) => void; errors: Record<string, string>; disabled?: boolean };

/** The fields of an agent definition. Tools (M4-B) are chosen from the registry only: the server validates and pins every one. */
export function AgentFields({ form, onChange, errors, disabled }: Props) {
  const text = (key: keyof AgentForm, label: string, hint?: string, rows?: number) => {
    const id = `f-${key}`;
    const shared = { id, value: form[key], disabled, "aria-invalid": !!errors[key], "aria-describedby": errors[key] ? `${id}-error` : undefined };
    return (
      <Field label={label} hint={hint} error={errors[key]} htmlFor={id}>
        {rows ? (
          <textarea className="st-textarea" style={{ minHeight: `${rows * 1.6}rem` }} {...shared} onChange={(e) => onChange({ [key]: e.target.value })} />
        ) : (
          <input className="st-input" {...shared} onChange={(e) => onChange({ [key]: e.target.value })} />
        )}
      </Field>
    );
  };
  return (
    <div className="grid gap-4">
      {text("name", "Name", "How this agent appears in plans, e.g. “Release Notes Writer”.")}
      {text("role", "Role", "A short title, e.g. “Product marketer”.")}
      {text("description", "Description", "What this agent is for, in a sentence or two.", 2)}
      {text("purpose", "Purpose", "The outcome it is responsible for.", 2)}
      {text("instructions", "Instructions", "How it should work. Be specific about tone, structure and what to avoid. Never paste secrets or API keys here.", 8)}
      {text("capabilities", "Capabilities", "Optional labels, comma separated (up to 12), e.g. release notes, changelog summaries.")}
      <div className="grid gap-4 sm:grid-cols-2">
        {text("input_expectations", "What it expects as input", "Optional.", 3)}
        {text("output_expectations", "What it produces", "Optional.", 3)}
      </div>
      <fieldset className="grid gap-3 rounded-lg border border-[var(--st-rule)] p-3" disabled={disabled}>
        <legend className="st-eyebrow px-1">Model</legend>
        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="Provider" htmlFor="f-provider" hint="Only Groq is supported today.">
            <select id="f-provider" className="st-select" value="groq" disabled>
              <option value="groq">Groq</option>
            </select>
          </Field>
          <Field label="Model" htmlFor="f-model" error={errors["model.name"]}>
            <input id="f-model" className="st-input st-mono" value={form.model_name} onChange={(e) => onChange({ model_name: e.target.value })} aria-invalid={!!errors["model.name"]} />
          </Field>
          <Field label="Temperature (0–2)" htmlFor="f-temp" error={errors["model.temperature"]} hint="Lower is steadier; higher is more varied.">
            <input id="f-temp" className="st-input st-mono" inputMode="decimal" value={form.temperature} onChange={(e) => onChange({ temperature: e.target.value })} aria-invalid={!!errors["model.temperature"]} />
          </Field>
        </div>
        <Field label="Max output tokens (optional)" htmlFor="f-max" error={errors["model.max_tokens"]} hint="Leave empty for the model default (up to 8192).">
          <input id="f-max" className="st-input st-mono sm:max-w-48" inputMode="numeric" value={form.max_tokens} onChange={(e) => onChange({ max_tokens: e.target.value })} aria-invalid={!!errors["model.max_tokens"]} />
        </Field>
      </fieldset>
      <fieldset className="grid gap-3 rounded-lg border border-[var(--st-rule)] p-3" disabled={disabled}>
        <legend className="st-eyebrow px-1">Tools</legend>
        <ToolPicker selected={form.tools} onChange={(tools) => onChange({ tools })} disabled={disabled} />
      </fieldset>
    </div>
  );
}

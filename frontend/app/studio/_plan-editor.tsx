"use client";

// A structured plan editor (M3-C). This is a form over the revision request, NOT a visual workflow builder: tasks are edited
// as fields, dependencies are chosen from the other tasks, and the backend validates the result (cycles, unknown or
// unavailable agents, ...) exactly like a model proposal. Saving creates a NEW revision and withdraws any approval.

import { useState } from "react";
import { getApi } from "@/lib/backend";
import type { AgentSummary, PlanDetail } from "@/lib/studio/types";
import { BUILTIN_AGENTS, blankTask, editorFromRevision, removeTask, validateEditor, type EditorTask } from "@/lib/studio/forms";
import { revisePlanFlow } from "@/lib/studio/flows";
import { explainError, type Explained } from "@/lib/studio/view";
import { ErrorNotice, Field, Spinner } from "./_ui";

export function PlanEditor({ plan, agents, onSaved, onCancel }: { plan: PlanDetail; agents: AgentSummary[]; onSaved: (plan: PlanDetail) => void; onCancel: () => void }) {
  const [tasks, setTasks] = useState<EditorTask[]>(() => editorFromRevision(plan.revision));
  const [interpretation, setInterpretation] = useState(plan.revision.interpretation);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [serverError, setServerError] = useState<Explained | null>(null);
  const [busy, setBusy] = useState(false);

  const patch = (id: string, change: Partial<EditorTask>) => setTasks((all) => all.map((t) => (t.id === id ? { ...t, ...change } : t)));
  const options = [
    ...BUILTIN_AGENTS.map((a) => ({ key: a.key, label: `${a.name} (built-in)` })),
    ...agents.map((a) => ({ key: a.slug, label: `${a.name} (custom, current v${a.current_version})` })),
  ];

  async function save() {
    const found = validateEditor(tasks);
    setErrors(found);
    if (Object.keys(found).length) return;
    setBusy(true);
    setServerError(null);
    try {
      onSaved(await revisePlanFlow(getApi(), plan, tasks, interpretation === plan.revision.interpretation ? null : interpretation));
    } catch (e) {
      setServerError(explainError(e, "The revision wasn't saved"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="st-panel grid gap-5 p-4 sm:p-5" aria-label="Edit plan tasks">
      <div>
        <div className="st-h2">Edit the plan</div>
        <p className="st-hint mt-1">
          Saving creates revision {plan.latest_revision + 1}; revision {plan.latest_revision} stays in the history. Any approval is withdrawn and
          you approve again. Agents are pinned to their current version when the revision is saved.
        </p>
      </div>
      <Field label="How AgentForge should read the objective (optional)" htmlFor="interp" error={errors._interp}>
        <textarea id="interp" className="st-textarea" value={interpretation} maxLength={600} onChange={(e) => setInterpretation(e.target.value)} />
      </Field>
      {errors._plan ? <div className="st-error-text">{errors._plan}</div> : null}
      {tasks.map((t, index) => {
        const others = tasks.filter((o) => o.id !== t.id);
        const e = (field: string) => errors[`${t.id}.${field}`];
        const known = options.some((o) => o.key === t.agent);
        return (
          <fieldset key={t.id} className="grid gap-3 rounded-lg border border-[var(--st-rule)] p-3">
            <legend className="st-eyebrow px-1">
              Task {index + 1} · <span className="st-mono">{t.id}</span>
              {t.isNew ? " · new" : ""}
            </legend>
            <Field label="Title" htmlFor={`${t.id}-title`} error={e("title")}>
              <input id={`${t.id}-title`} className="st-input" value={t.title} onChange={(ev) => patch(t.id, { title: ev.target.value })} aria-invalid={!!e("title")} />
            </Field>
            <Field label="What the agent should do" htmlFor={`${t.id}-desc`} error={e("description")}>
              <textarea id={`${t.id}-desc`} className="st-textarea" value={t.description} onChange={(ev) => patch(t.id, { description: ev.target.value })} aria-invalid={!!e("description")} />
            </Field>
            <Field label="What it should deliver" htmlFor={`${t.id}-out`} error={e("expected_output")}>
              <textarea id={`${t.id}-out`} className="st-textarea" style={{ minHeight: "3.5rem" }} value={t.expected_output} onChange={(ev) => patch(t.id, { expected_output: ev.target.value })} aria-invalid={!!e("expected_output")} />
            </Field>
            <div className="grid gap-3 sm:grid-cols-3">
              <Field label="Agent" htmlFor={`${t.id}-agent`} error={e("agent")}>
                <select id={`${t.id}-agent`} className="st-select" value={t.agent} onChange={(ev) => patch(t.id, { agent: ev.target.value })}>
                  {!known ? <option value={t.agent}>{t.agent} (unavailable)</option> : null}
                  {options.map((o) => (
                    <option key={o.key} value={o.key}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Complexity" htmlFor={`${t.id}-cx`}>
                <select id={`${t.id}-cx`} className="st-select" value={t.complexity} onChange={(ev) => patch(t.id, { complexity: ev.target.value as EditorTask["complexity"] })}>
                  {["low", "medium", "high"].map((v) => (
                    <option key={v}>{v}</option>
                  ))}
                </select>
              </Field>
              <Field label="Priority" htmlFor={`${t.id}-pr`}>
                <select id={`${t.id}-pr`} className="st-select" value={t.priority} onChange={(ev) => patch(t.id, { priority: ev.target.value as EditorTask["priority"] })}>
                  {["low", "normal", "high"].map((v) => (
                    <option key={v}>{v}</option>
                  ))}
                </select>
              </Field>
            </div>
            {others.length ? (
              <div role="group" aria-label={`Tasks that ${t.title || t.id} waits for`}>
                <div className="st-label">Waits for</div>
                <div className="mt-1 flex flex-wrap gap-2">
                  {others.map((o) => (
                    <label key={o.id} className="st-chip cursor-pointer">
                      <input
                        type="checkbox"
                        checked={t.depends_on.includes(o.id)}
                        onChange={(ev) => patch(t.id, { depends_on: ev.target.checked ? [...t.depends_on, o.id] : t.depends_on.filter((d) => d !== o.id) })}
                      />
                      {o.title || o.id}
                    </label>
                  ))}
                </div>
                {e("depends_on") ? <div className="st-error-text">{e("depends_on")}</div> : null}
              </div>
            ) : null}
            <div>
              <button type="button" className="st-btn st-btn-sm st-btn-danger" onClick={() => setTasks((all) => removeTask(all, t.id))} disabled={tasks.length <= 1}>
                Remove task
              </button>
            </div>
          </fieldset>
        );
      })}
      <div>
        <button type="button" className="st-btn st-btn-sm" onClick={() => setTasks((all) => [...all, blankTask(all)])} disabled={tasks.length >= 12}>
          + Add a task
        </button>
      </div>
      {serverError ? <ErrorNotice error={serverError} /> : null}
      <div className="flex flex-wrap gap-2">
        <button className="st-btn st-btn-primary" onClick={() => void save()} disabled={busy}>
          {busy ? <Spinner /> : null}Save as revision {plan.latest_revision + 1}
        </button>
        <button className="st-btn" onClick={onCancel} disabled={busy}>
          Discard changes
        </button>
      </div>
    </div>
  );
}

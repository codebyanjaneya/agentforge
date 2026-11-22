"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { getApi } from "@/lib/backend";
import { createAgentFlow } from "@/lib/studio/flows";
import { blankForm, suggestSlug, validateForm, validateSlug, type AgentForm } from "@/lib/studio/forms";
import { explainError, type Explained } from "@/lib/studio/view";
import { AgentFields } from "../../_agent-form";
import { BackLink, ErrorNotice, Field, PageHead, Spinner, useFocusFirstError } from "../../_ui";

export default function NewAgentPage() {
  const router = useRouter();
  const [form, setForm] = useState<AgentForm>(blankForm);
  const [slug, setSlug] = useState("");
  const [slugTouched, setSlugTouched] = useState(false);
  const [note, setNote] = useState("");
  const [errors, setErrors] = useState<Record<string, string>>({});
  useFocusFirstError(errors);
  const [serverError, setServerError] = useState<Explained | null>(null);
  const [busy, setBusy] = useState(false);

  function change(patch: Partial<AgentForm>) {
    setForm((f) => ({ ...f, ...patch }));
    if (patch.name !== undefined && !slugTouched) setSlug(suggestSlug(patch.name));
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const found = validateForm(form);
    const slugProblem = validateSlug(slug);
    if (slugProblem) found.slug = slugProblem;
    setErrors(found);
    setServerError(null);
    if (Object.keys(found).length) return;
    setBusy(true);
    try {
      const agent = await createAgentFlow(getApi(), slug, form, note);
      router.push(`/studio/agents/${agent.id}`);
    } catch (e) {
      const explained = explainError(e, "The agent wasn't created");
      setServerError(explained);
      setErrors(explained.fields);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto max-w-3xl">
      <BackLink href="/studio/agents">Agents</BackLink>
      <div className="mt-3">
        <PageHead eyebrow="New agent" title="Create a custom agent" lede="Define a specialist once; plans can then assign it to tasks. Each save creates an immutable version." />
      </div>
      <form onSubmit={submit} className="mt-6 grid gap-5" noValidate>
        <Field label="Identifier" htmlFor="f-slug" error={errors.slug} hint="Used to refer to this agent (lowercase letters, digits, hyphens). It cannot be changed later.">
          <input id="f-slug" className="st-input st-mono" value={slug} onChange={(e) => { setSlugTouched(true); setSlug(e.target.value); }} aria-invalid={!!errors.slug} />
        </Field>
        <AgentFields form={form} onChange={change} errors={errors} disabled={busy} />
        <Field label="Note for the history (optional)" htmlFor="f-note">
          <input id="f-note" className="st-input" value={note} maxLength={200} onChange={(e) => setNote(e.target.value)} />
        </Field>
        {serverError ? <ErrorNotice error={serverError} /> : null}
        <div className="flex gap-2">
          <button className="st-btn st-btn-primary" type="submit" disabled={busy}>
            {busy ? <Spinner /> : null}Create agent
          </button>
          <button className="st-btn" type="button" onClick={() => router.push("/studio/agents")} disabled={busy}>
            Cancel
          </button>
        </div>
      </form>
    </div>
  );
}

"use client";

import { useParams, useRouter } from "next/navigation";
import { useState } from "react";
import { getApi } from "@/lib/backend";
import { suggestSlug, validateSlug } from "@/lib/studio/forms";
import { explainError, type Explained } from "@/lib/studio/view";
import { BackLink, ErrorNotice, Field, PageHead, Section, Skeleton, Spinner, useLoader } from "../../_ui";

export default function TemplatePage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const template = useLoader(() => getApi().getTemplate(id), [id]);
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Explained | null>(null);

  const t = template.data;
  const effectiveSlug = slug || (t ? t.id : "");
  const slugError = slug ? validateSlug(slug) : null;

  async function create(event: React.FormEvent) {
    event.preventDefault();
    if (slugError || busy) return;
    setBusy(true);
    setError(null);
    try {
      // Only optional naming is sent: the owner, workspace and definition come from the server and the template itself.
      const agent = await getApi().instantiateTemplate(id, { ...(slug ? { slug } : {}), ...(name.trim() ? { name: name.trim() } : {}) });
      router.push(`/studio/agents/${agent.id}`);
    } catch (e) {
      setError(explainError(e, "Couldn't create the agent"));
    } finally {
      setBusy(false);
    }
  }

  if (template.loading) return <div className="mx-auto max-w-4xl"><Skeleton lines={4} /></div>;
  if (template.error || !t) {
    return (
      <div className="mx-auto max-w-4xl">
        <BackLink href="/studio/templates">Templates</BackLink>
        <h1 className="sr-only">Template</h1>
        <div className="mt-4">{template.error ? <ErrorNotice error={template.error} onRetry={template.reload} /> : null}</div>
      </div>
    );
  }
  const d = t.definition;
  return (
    <div className="mx-auto max-w-4xl">
      <BackLink href="/studio/templates">Templates</BackLink>
      <div className="mt-3">
        <PageHead eyebrow={`Template · version ${t.version}`} title={t.name} lede={t.description} />
      </div>
      <div className="st-split">
        <div>
          <Section title="What it does">
            <dl className="mt-3 grid gap-4 text-sm">
              <div>
                <dt className="st-eyebrow">Role</dt>
                <dd className="mt-1">{d.role}</dd>
              </div>
              <div>
                <dt className="st-eyebrow">Purpose</dt>
                <dd className="mt-1">{d.purpose}</dd>
              </div>
              {t.capabilities.length ? (
                <div>
                  <dt className="st-eyebrow">Capabilities</dt>
                  <dd className="st-task-meta">
                    {t.capabilities.map((c) => (
                      <span key={c} className="st-chip">
                        {c}
                      </span>
                    ))}
                  </dd>
                </div>
              ) : null}
              {d.input_expectations ? (
                <div>
                  <dt className="st-eyebrow">Expects</dt>
                  <dd className="mt-1">{d.input_expectations}</dd>
                </div>
              ) : null}
              {d.output_expectations ? (
                <div>
                  <dt className="st-eyebrow">Produces</dt>
                  <dd className="mt-1">{d.output_expectations}</dd>
                </div>
              ) : null}
            </dl>
          </Section>
          <Section title="Instructions it will follow">
            <pre className="st-output" tabIndex={0}>{d.instructions}</pre>
            <p className="st-hint mt-2">
              Model: <span className="st-mono">{d.model.provider} · {d.model.name}</span>. Tools: none are available yet, so this agent only produces written responses.
            </p>
          </Section>
        </div>
        <aside>
          <Section title="Use this template">
            <form onSubmit={create} className="grid gap-4 pt-4">
              <Field label="Name (optional)" htmlFor="tpl-name" hint="Defaults to the template's name.">
                <input id="tpl-name" className="st-input" value={name} onChange={(e) => { setName(e.target.value); if (!touched) setSlug(suggestSlug(e.target.value)); }} />
              </Field>
              <Field label="Identifier (optional)" htmlFor="tpl-slug" error={slugError ?? undefined} hint={`Used to refer to the agent. Defaults to “${effectiveSlug}”.`}>
                <input id="tpl-slug" className="st-input st-mono" value={slug} onChange={(e) => { setTouched(true); setSlug(e.target.value); }} aria-invalid={!!slugError} />
              </Field>
              {error ? <ErrorNotice error={error} /> : null}
              <button className="st-btn st-btn-primary" type="submit" disabled={busy || !!slugError}>
                {busy ? <Spinner /> : null}Create my agent
              </button>
              <p className="st-hint">You get your own editable copy at version 1. The template is never modified.</p>
            </form>
          </Section>
        </aside>
      </div>
    </div>
  );
}

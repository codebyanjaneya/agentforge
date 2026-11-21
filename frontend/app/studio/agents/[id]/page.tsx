"use client";

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { getApi } from "@/lib/backend";
import { rollbackFlow, saveAgentFlow } from "@/lib/studio/flows";
import { formFromDefinition, validateForm, type AgentForm } from "@/lib/studio/forms";
import type { AgentDetail, VersionDetail } from "@/lib/studio/types";
import { explainError, relativeTime, type Explained } from "@/lib/studio/view";
import { AgentFields } from "../../_agent-form";
import { BackLink, Badge, ConfirmDialog, EmptyState, ErrorNotice, Field, Notice, PageHead, Section, Skeleton, Spinner, useLoader } from "../../_ui";

type Tab = "edit" | "versions";

export default function AgentPage() {
  const { id } = useParams<{ id: string }>();
  const agent = useLoader(() => getApi().getAgent(id), [id]);
  const versions = useLoader(() => getApi().listVersions(id), [id, agent.data?.latest_version]);
  const [tab, setTab] = useState<Tab>("edit");

  const [form, setForm] = useState<AgentForm | null>(null);
  const [note, setNote] = useState("");
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [serverError, setServerError] = useState<Explained | null>(null);
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const [dialog, setDialog] = useState<null | "archive" | { rollback: number }>(null);
  const [viewing, setViewing] = useState<VersionDetail | null>(null);
  const [viewError, setViewError] = useState<Explained | null>(null);

  const a = agent.data;
  // (Re)initialise the editable form whenever a different version of the agent is loaded.
  useEffect(() => {
    if (a) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setForm(formFromDefinition(a.definition));
      setErrors({});
    }
  }, [a?.id, a?.latest_version, a?.current_version]); // eslint-disable-line react-hooks/exhaustive-deps

  function adopt(next: AgentDetail) {
    agent.setData(next);
    setServerError(null);
  }

  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (!a || !form) return;
    const found = validateForm(form);
    setErrors(found);
    setSaved(false);
    setServerError(null);
    if (Object.keys(found).length) return;
    setBusy(true);
    try {
      adopt(await saveAgentFlow(getApi(), a, form, note));
      setNote("");
      setSaved(true);
    } catch (e) {
      const explained = explainError(e, "Your changes weren't saved");
      setServerError(explained);
      setErrors(explained.fields);
    } finally {
      setBusy(false);
    }
  }

  async function archiveToggle() {
    if (!a) return;
    setBusy(true);
    try {
      adopt(a.status === "active" ? await getApi().archiveAgent(a.id) : await getApi().unarchiveAgent(a.id));
    } catch (e) {
      setServerError(explainError(e));
    } finally {
      setBusy(false);
      setDialog(null);
    }
  }

  async function rollback(to: number) {
    if (!a) return;
    setBusy(true);
    try {
      adopt(await rollbackFlow(getApi(), a, to));
      setViewing(null);
      setTab("versions");
    } catch (e) {
      setServerError(explainError(e, "The version wasn't restored"));
    } finally {
      setBusy(false);
      setDialog(null);
    }
  }

  async function view(version: number) {
    setViewError(null);
    try {
      setViewing(await getApi().getVersion(id, version));
    } catch (e) {
      setViewError(explainError(e));
    }
  }

  if (agent.loading && !a) return <div className="mx-auto max-w-3xl"><Skeleton lines={5} height="3.4rem" /></div>;
  if (agent.error || !a || !form) {
    return (
      <div className="mx-auto max-w-3xl">
        <BackLink href="/studio/agents">Agents</BackLink>
        <div className="mt-4">{agent.error ? <ErrorNotice error={agent.error} onRetry={agent.reload} /> : <Skeleton />}</div>
      </div>
    );
  }
  const archived = a.status === "archived";

  return (
    <div className="mx-auto max-w-3xl">
      <BackLink href="/studio/agents">Agents</BackLink>
      <div className="mt-3">
        <PageHead
          eyebrow={`Custom agent · ${a.slug}`}
          title={a.name}
          lede={a.description}
          actions={
            <>
              {archived ? <Badge tone="muted">Archived</Badge> : null}
              <span className="st-chip st-mono">version {a.current_version}</span>
              <button className={`st-btn ${archived ? "" : "st-btn-danger"}`} onClick={() => (archived ? void archiveToggle() : setDialog("archive"))} disabled={busy}>
                {archived ? "Unarchive" : "Archive"}
              </button>
            </>
          }
        />
      </div>
      {a.template_id ? <p className="st-hint mt-2">Created from the “{a.template_id}” template (version {a.template_version}). Your copy is independent.</p> : null}

      <div role="tablist" aria-label="Agent sections" className="mt-6 flex gap-1 border-b border-[var(--st-rule-2)]">
        {(["edit", "versions"] as Tab[]).map((t) => (
          <button key={t} role="tab" aria-selected={tab === t} className="st-btn" style={{ border: 0, borderBottom: `2px solid ${tab === t ? "var(--st-ember)" : "transparent"}`, borderRadius: 0 }} onClick={() => setTab(t)}>
            {t === "edit" ? "Definition" : `Version history · ${a.latest_version}`}
          </button>
        ))}
      </div>

      {tab === "edit" ? (
        <form onSubmit={save} className="mt-6 grid gap-5" noValidate>
          {archived ? <Notice tone="warn" title="This agent is archived">It cannot be edited or used in new plans until you unarchive it. Plans that already pinned a version keep it.</Notice> : null}
          <Notice tone="info" title="How editing works">
            <span className="text-sm">
              Saving creates version {a.latest_version + 1}. Earlier versions are never changed, and plans keep using the exact version they were approved with.
            </span>
          </Notice>
          <AgentFields form={form} onChange={(patch) => { setForm({ ...form, ...patch }); setSaved(false); }} errors={errors} disabled={archived || busy} />
          <Field label="Note for the history (optional)" htmlFor="f-note">
            <input id="f-note" className="st-input" value={note} maxLength={200} onChange={(e) => setNote(e.target.value)} disabled={archived || busy} />
          </Field>
          {serverError ? <ErrorNotice error={serverError} onRetry={serverError.stale ? () => { agent.reload(); setServerError(null); } : undefined} /> : null}
          {saved ? <Notice tone="good" title={`Saved as version ${a.latest_version}`} /> : null}
          <div>
            <button className="st-btn st-btn-primary" type="submit" disabled={archived || busy}>
              {busy ? <Spinner /> : null}Save as version {a.latest_version + 1}
            </button>
          </div>
        </form>
      ) : (
        <Section title="Immutable versions" aside="Restoring creates a new version; nothing is rewritten.">
          {versions.loading && !versions.data ? (
            <Skeleton lines={3} height="2.8rem" />
          ) : versions.error ? (
            <ErrorNotice error={versions.error} onRetry={versions.reload} />
          ) : !versions.data || versions.data.items.length === 0 ? (
            <EmptyState title="No versions" />
          ) : (
            <ul className="m-0 list-none p-0">
              {versions.data.items.map((v) => (
                <li key={v.version} className="st-row" style={{ gridTemplateColumns: "minmax(0,1fr) auto" }}>
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="st-mono font-semibold">v{v.version}</span>
                      {v.version === a.current_version ? <Badge tone="good">current</Badge> : null}
                      {v.restored_from_version ? <span className="st-chip">restored from v{v.restored_from_version}</span> : null}
                    </div>
                    <div className="st-muted text-xs">
                      {v.change_note ?? "No note"} · {relativeTime(v.created_at)} · <span className="st-mono">{v.content_hash.slice(0, 8)}</span>
                    </div>
                  </div>
                  <div className="flex gap-2">
                    <button className="st-btn st-btn-sm" onClick={() => void view(v.version)}>
                      View
                    </button>
                    {v.version !== a.current_version && !archived ? (
                      <button className="st-btn st-btn-sm" onClick={() => setDialog({ rollback: v.version })} disabled={busy}>
                        Restore
                      </button>
                    ) : null}
                  </div>
                </li>
              ))}
            </ul>
          )}
          {viewError ? <ErrorNotice error={viewError} /> : null}
          {serverError ? <div className="mt-3"><ErrorNotice error={serverError} onRetry={serverError.stale ? () => { agent.reload(); setServerError(null); } : undefined} /></div> : null}
          {viewing ? (
            <div className="st-panel mt-4 grid gap-3 p-4">
              <div className="flex items-center justify-between">
                <div className="st-h2">Version {viewing.version} (read-only)</div>
                <button className="st-btn st-btn-sm" onClick={() => setViewing(null)}>
                  Close
                </button>
              </div>
              <dl className="grid gap-3 text-sm">
                {[
                  ["Name", viewing.definition.name], ["Role", viewing.definition.role], ["Description", viewing.definition.description], ["Purpose", viewing.definition.purpose],
                  ["Model", `${viewing.definition.model.provider} · ${viewing.definition.model.name} · temperature ${viewing.definition.model.temperature}`],
                ].map(([k, v]) => (
                  <div key={k}>
                    <dt className="st-eyebrow">{k}</dt>
                    <dd className="mt-1">{v}</dd>
                  </div>
                ))}
              </dl>
              <div>
                <div className="st-eyebrow">Instructions</div>
                <pre className="st-output" tabIndex={0}>{viewing.definition.instructions}</pre>
              </div>
            </div>
          ) : null}
        </Section>
      )}

      <ConfirmDialog open={dialog === "archive"} title="Archive this agent?" confirmLabel="Archive" tone="danger" busy={busy} onCancel={() => setDialog(null)} onConfirm={() => void archiveToggle()}>
        <p>Archived agents can&apos;t be edited or assigned in new plans, and runs that would need them are refused. You can unarchive at any time. Plans that pinned a version keep it.</p>
      </ConfirmDialog>
      <ConfirmDialog
        open={typeof dialog === "object" && dialog !== null}
        title={typeof dialog === "object" && dialog ? `Restore version ${dialog.rollback}?` : ""}
        confirmLabel="Restore as a new version"
        busy={busy}
        onCancel={() => setDialog(null)}
        onConfirm={() => typeof dialog === "object" && dialog ? void rollback(dialog.rollback) : undefined}
      >
        <p>
          This creates version {a.latest_version + 1} with the content of version {typeof dialog === "object" && dialog ? dialog.rollback : ""}. Existing versions, and plans that pinned them, are unchanged.
        </p>
      </ConfirmDialog>
    </div>
  );
}

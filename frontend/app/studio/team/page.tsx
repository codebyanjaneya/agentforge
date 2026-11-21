"use client";

import { useState, type FormEvent } from "react";
import { getApi } from "@/lib/backend";
import { describeError } from "@/lib/api-client";
import { selectWorkspace } from "@/lib/workspace";
import { EmptyState, Notice, PageHead, Section, Skeleton, useLoader } from "../_ui";
import { Members } from "../_members";

/** Shared workspaces: who is in them and what each role may do. Roles come from the server; nothing here grants anything. */
export default function TeamPage() {
  const [version, setVersion] = useState(0);
  const list = useLoader(() => getApi().listWorkspaces(), [version]);
  const [name, setName] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function create(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setProblem(null);
    try {
      const made = await getApi().createWorkspace(name.trim());
      setName("");
      selectWorkspace(made.id);
      window.location.assign("/studio/team");
    } catch (err) {
      setProblem(describeError(err, "Couldn't create the workspace."));
      setBusy(false);
      setVersion((v) => v + 1);
    }
  }

  const shared = (list.data?.items ?? []).filter((w) => !w.personal);
  return (
    <div className="mx-auto max-w-4xl">
      <PageHead
        eyebrow="Team"
        title="Work with other people"
        lede="A shared workspace has one owner and any number of admins and members. Everyone in it sees the workspace's plans, runs, results and approvals; only the person who created a plan or started a run can change it. Owners and admins decide approvals."
      />
      <Section title="Create a shared workspace">
        <form className="grid gap-3 sm:grid-cols-[1fr_auto] sm:items-end" onSubmit={create}>
          <div className="st-field">
            <label className="st-label" htmlFor="workspace-name">
              Name
            </label>
            <input id="workspace-name" className="st-input" required maxLength={100} value={name} onChange={(e) => setName(e.target.value)} autoComplete="off" />
          </div>
          <button className="st-btn st-btn-primary" disabled={busy || !name.trim()}>
            {busy ? "Creating…" : "Create workspace"}
          </button>
        </form>
        <div aria-live="polite">{problem ? <Notice tone="bad" title="Couldn't create it">{problem}</Notice> : null}</div>
      </Section>
      {list.data ? (
        shared.length ? (
          shared.map((w) => (
            <Section key={w.id} title={w.name} aside={`You are ${w.role} · ${w.members} member${w.members === 1 ? "" : "s"}`}>
              <Members workspace={w} />
            </Section>
          ))
        ) : (
          <EmptyState title="You are not in a shared workspace yet">Create one above, then add the people you work with.</EmptyState>
        )
      ) : (
        <Skeleton lines={3} height="3rem" />
      )}
    </div>
  );
}

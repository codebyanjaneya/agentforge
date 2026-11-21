"use client";

import { useState, type FormEvent } from "react";
import { getApi } from "@/lib/backend";
import { describeError } from "@/lib/api-client";
import type { WorkspaceInfo } from "@/lib/studio/types";
import { selectWorkspace } from "@/lib/workspace";
import { Notice, Skeleton, useLoader } from "./_ui";

const ROLE_TEXT = { owner: "Owner: can invite people and decide approvals", admin: "Admin: can decide approvals", member: "Member: can see requests, cannot decide them" } as const;

/** Members, invitations and role management of one shared workspace. Every control is a request: the server re-checks the owner role (and the sole-owner rule) each time. */
export function Members({ workspace }: { workspace: WorkspaceInfo }) {
  const [version, setVersion] = useState(0);
  const members = useLoader(() => getApi().listWorkspaceMembers(workspace.id), [workspace.id, version]);
  const owner = workspace.role === "owner";
  const pending = useLoader(() => (owner ? getApi().listInvitations(workspace.id) : Promise.resolve({ items: [] })), [workspace.id, version, owner]);
  const [invitee, setInvitee] = useState("");
  const [role, setRole] = useState<"admin" | "member">("member");
  const [problem, setProblem] = useState<string | null>(null);
  const [link, setLink] = useState<{ email: string; url: string } | null>(null);
  const [busy, setBusy] = useState(false);

  async function act(fn: () => Promise<unknown>, fallback: string) {
    setProblem(null);
    try {
      await fn();
      setVersion((v) => v + 1);
    } catch (err) {
      setProblem(describeError(err, fallback));
    }
  }

  async function invite(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setProblem(null);
    setLink(null);
    try {
      const made = await getApi().inviteToWorkspace(workspace.id, invitee.trim(), role);
      setLink({ email: made.email, url: `${window.location.origin}/studio/join?token=${encodeURIComponent(made.token)}` });
      setInvitee("");
      setVersion((v) => v + 1);
    } catch (err) {
      setProblem(describeError(err, "Couldn't create the invitation."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      {members.data ? (
        <ul className="st-appr-list" aria-label={`Members of ${workspace.name}`}>
          {members.data.items.map((m) => (
            <li key={m.email} className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
              <span>{m.email}</span>
              {owner && m.role !== "owner" ? (
                <span className="flex items-center gap-2">
                  <select
                    className="st-input"
                    aria-label={`Role of ${m.email}`}
                    value={m.role}
                    onChange={(e) => void act(() => getApi().changeMemberRole(workspace.id, m.email, e.target.value as "admin" | "member"), "Couldn't change the role.")}
                  >
                    <option value="member">Member</option>
                    <option value="admin">Admin</option>
                  </select>
                  <button className="st-btn" onClick={() => void act(() => getApi().removeMember(workspace.id, m.email), "Couldn't remove that person.")}>
                    Remove
                  </button>
                </span>
              ) : (
                <span className="st-muted">{ROLE_TEXT[m.role]}</span>
              )}
            </li>
          ))}
        </ul>
      ) : (
        <Skeleton lines={2} height="2rem" />
      )}
      {owner ? (
        <>
          <form className="mt-4 grid gap-3 sm:grid-cols-[1fr_auto_auto] sm:items-end" onSubmit={invite}>
            <div className="st-field">
              <label className="st-label" htmlFor={`invitee-${workspace.id}`}>
                Invite by email (they do not need an account yet)
              </label>
              <input id={`invitee-${workspace.id}`} className="st-input" type="email" required value={invitee} onChange={(e) => setInvitee(e.target.value)} autoComplete="off" />
            </div>
            <div className="st-field">
              <label className="st-label" htmlFor={`role-${workspace.id}`}>
                Role
              </label>
              <select id={`role-${workspace.id}`} className="st-input" value={role} onChange={(e) => setRole(e.target.value as "admin" | "member")}>
                <option value="member">Member</option>
                <option value="admin">Admin</option>
              </select>
            </div>
            <button className="st-btn st-btn-primary" disabled={busy || !invitee.trim()}>
              {busy ? "Creating…" : "Create invitation"}
            </button>
          </form>
          {pending.data && pending.data.items.length ? (
            <ul className="st-appr-list mt-3" aria-label="Pending invitations">
              {pending.data.items.map((i) => (
                <li key={i.id} className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
                  <span>
                    {i.email} <span className="st-muted">· {i.role} · expires {new Date(i.expires_at + "Z").toLocaleDateString()}</span>
                  </span>
                  <button className="st-btn" onClick={() => void act(() => getApi().revokeInvitation(workspace.id, i.id), "Couldn't withdraw it.")}>
                    Withdraw
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
        </>
      ) : (
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <p className="st-muted m-0 text-sm">Only the owner can invite people.</p>
          <button
            className="st-btn"
            onClick={() =>
              void act(async () => {
                await getApi().leaveWorkspace(workspace.id);
                selectWorkspace(null);
                window.location.assign("/studio/team");
              }, "Couldn't leave the workspace.")
            }
          >
            Leave workspace
          </button>
        </div>
      )}
      <div aria-live="polite">
        {problem ? (
          <Notice tone="bad" title="That didn't work">
            {problem}
          </Notice>
        ) : null}
        {link ? (
          <Notice tone="good" title={`Invitation for ${link.email}`}>
            AgentForge does not send email: copy this link and send it yourself. It works once, only for that email address, and is shown only now.
            <input className="st-input mt-2" readOnly aria-label="Invitation link" value={link.url} onFocus={(e) => e.currentTarget.select()} />
          </Notice>
        ) : null}
      </div>
    </div>
  );
}

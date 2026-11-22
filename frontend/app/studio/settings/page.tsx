"use client";

import Link from "next/link";
import { getApi } from "@/lib/backend";
import { activeWorkspace } from "@/lib/workspace";
import { useMyEmail } from "../_me";
import { PageHead, Section, Skeleton, useLoader } from "../_ui";

/** What each role may do. This is the product's real policy (the server enforces it on every request); the table only states it. */
const MATRIX: { what: string; owner: boolean; admin: boolean; member: boolean; note?: string }[] = [
  { what: "See the workspace's plans, runs, usage, tool calls and approvals", owner: true, admin: true, member: true },
  { what: "Create a plan or start a run", owner: true, admin: true, member: true, note: "each person's own; others can read it" },
  { what: "Revise, approve, cancel or retry", owner: true, admin: true, member: true, note: "only the person who created the plan or started the run" },
  { what: "Decide an approval request (exact action)", owner: true, admin: true, member: false },
  { what: "Reconcile an unknown side-effect outcome", owner: true, admin: true, member: false },
  { what: "Invite people, change roles, remove members", owner: true, admin: false, member: false },
  { what: "Leave the workspace", owner: false, admin: true, member: true, note: "the sole owner cannot leave" },
];

const mark = (yes: boolean) => (yes ? <span aria-label="allowed">Yes</span> : <span className="st-muted" aria-label="not allowed">No</span>);

export default function SettingsPage() {
  const me = useMyEmail();
  const list = useLoader(() => getApi().listWorkspaces(), []);
  const demo = useLoader(() => getApi().getDemo(), []);
  const chosen = activeWorkspace();
  const items = list.data?.items ?? [];
  const here = items.find((w) => w.id === chosen) ?? items.find((w) => w.personal);
  return (
    <div className="mx-auto max-w-4xl">
      <PageHead eyebrow="Workspace" title="Settings" lede="Where you are working, in what capacity, and what that allows. Roles come from the server; nothing on this page grants anything." />
      <Section title="This workspace">
        {list.data ? (
          <dl className="st-spec" aria-label="Workspace facts">
            <div>
              <dt>Name</dt>
              <dd>{here ? (here.personal ? "Personal" : here.name) : "Unknown"}</dd>
            </div>
            <div>
              <dt>Your role</dt>
              <dd>{here?.role ?? "Unknown"}</dd>
            </div>
            <div>
              <dt>Members</dt>
              <dd>{here?.members ?? "?"}</dd>
            </div>
            <div>
              <dt>Signed in as</dt>
              <dd>{me ?? "Loading"}</dd>
            </div>
          </dl>
        ) : (
          <Skeleton lines={2} height="2.5rem" />
        )}
        <p className="st-hint mt-2">
          {here && !here.personal ? (
            <>
              Manage members and invitations on the <Link href="/studio/team">Team page</Link>.
            </>
          ) : (
            <>
              This is your personal workspace. To work with other people, <Link href="/studio/team">create a shared workspace</Link>.
            </>
          )}
        </p>
      </Section>
      <Section title="Model and tools" aside="What this deployment is configured to use">
        {demo.data ? (
          <dl className="st-spec" aria-label="Model and tools">
            <div>
              <dt>Mode</dt>
              <dd>{demo.data.demo ? "Demo (scripted model, simulated tool)" : "Live"}</dd>
            </div>
            <div>
              <dt>Configured model</dt>
              <dd className="st-mono">{demo.data.model_name ?? "not reported"}</dd>
            </div>
            <div>
              <dt>Real lookups</dt>
              <dd className="st-mono">{demo.data.live_tools.length ? demo.data.live_tools.join(", ") : "none"}</dd>
            </div>
          </dl>
        ) : (
          <Skeleton lines={1} height="2.5rem" />
        )}
        <p className="st-hint mt-2">This is the configured model name, not proof that the provider is currently serving it; each run records what actually answered.</p>
      </Section>
      <Section title="What each role may do">
        <div className="st-table-wrap" role="region" aria-label="Role capabilities: scrollable" tabIndex={0}>
          <table className="st-table w-full text-sm">
            <caption className="sr-only">What owner, admin and member may each do in a shared workspace</caption>
            <thead>
              <tr>
                <th scope="col">Capability</th>
                <th scope="col">Owner</th>
                <th scope="col">Admin</th>
                <th scope="col">Member</th>
              </tr>
            </thead>
            <tbody>
              {MATRIX.map((row) => (
                <tr key={row.what}>
                  <th scope="row">
                    {row.what}
                    {row.note ? <span className="st-hint block">{row.note}</span> : null}
                  </th>
                  <td>{mark(row.owner)}</td>
                  <td>{mark(row.admin)}</td>
                  <td>{mark(row.member)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="st-hint mt-2">The server decides on every request; hiding a button on another page is a courtesy, never the control.</p>
      </Section>
    </div>
  );
}

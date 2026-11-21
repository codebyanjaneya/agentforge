"use client";

import { useEffect, useState, useSyncExternalStore } from "react";
import { getApi } from "@/lib/backend";
import { selectWorkspace } from "@/lib/workspace";
import { Notice, PageHead, Section } from "../_ui";

let captured: string | null | undefined; // read once on the client; the secret then leaves the address bar and history
const readToken = () => {
  if (captured === undefined) {
    captured = new URLSearchParams(window.location.search).get("token");
  }
  return captured;
};
const noSubscribe = () => () => {};

/** Accepting an invitation. The link carries the secret as `?token=`; /studio is behind the sign-in gate whose callbackUrl keeps the query, so a person who has not signed in yet
 *  signs in and lands back here. The server only accepts it for the signed-in person whose verified e-mail was invited; every failure looks the same. */
export default function JoinPage() {
  const token = useSyncExternalStore(noSubscribe, readToken, () => null);
  useEffect(() => {
    if (window.location.search) window.history.replaceState(null, "", "/studio/join"); // after render: Next listens to history changes
  }, []);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState(false);
  const [joined, setJoined] = useState<string | null>(null);

  async function join() {
    if (!token) return;
    setBusy(true);
    setProblem(false);
    try {
      const r = await getApi().acceptInvitation(token);
      selectWorkspace(r.workspace_id);
      setJoined(r.name);
    } catch {
      setProblem(true);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto max-w-xl">
      <PageHead eyebrow="Invitation" title="Join a shared workspace" lede="You were invited to work together. Sign in with the email address the invitation was sent to." />
      <Section title="Accept">
        {joined ? (
          <Notice tone="good" title={`You joined ${joined}`}>
            <a className="st-btn st-btn-primary" href="/studio">
              Open Studio
            </a>
          </Notice>
        ) : token ? (
          <button className="st-btn st-btn-primary" onClick={() => void join()} disabled={busy}>
            {busy ? "Joining…" : "Join workspace"}
          </button>
        ) : (
          <Notice tone="bad" title="This link has no invitation">
            Ask the workspace owner for a new invitation link.
          </Notice>
        )}
        <div aria-live="polite">
          {problem ? (
            <Notice tone="bad" title="This invitation can't be used">
              It may have expired, been withdrawn or already been used, or it was sent to a different email address than the one you signed in with.
            </Notice>
          ) : null}
        </div>
      </Section>
    </div>
  );
}

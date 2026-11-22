"use client";

import { useEffect, useRef, useState } from "react";
import { getApi } from "@/lib/backend";
import { createPoller } from "@/lib/studio/poller";
import type { NotificationCounts, WorkspaceInfo } from "@/lib/studio/types";
import { activeWorkspace, selectWorkspace, WORKSPACE_EVENT } from "@/lib/workspace";

export const INBOX_EVENT = "agentforge:inbox";
/** Ask the badge to re-read now (after a decision, a read, a new workspace). */
export const refreshInbox = () => window.dispatchEvent(new Event(INBOX_EVENT));

/** The inbox badge: ONE bounded poller (15 s, paused while the tab is hidden, one request in flight) over `GET /notifications/count`, plus an immediate refresh on demand.
 *  `pending` is what is still waiting for a decision, so it falls after approve, reject, expiry or invalidation. */
export function useInboxCount(): { counts: NotificationCounts | null; announcement: string } {
  const [counts, setCounts] = useState<NotificationCounts | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const last = useRef<number | null>(null);
  useEffect(() => {
    const poller = createPoller<NotificationCounts>({
      fetch: () => getApi().getNotificationCounts(),
      onData: (value) => {
        setCounts(value);
        if (last.current !== null && value.pending !== last.current) {
          setAnnouncement(value.pending === 0 ? "No approvals are waiting." : `${value.pending} approval${value.pending === 1 ? " is" : "s are"} waiting for a decision.`);
        }
        last.current = value.pending;
      },
      isTerminal: () => false,
      intervalMs: 15000,
    });
    poller.start();
    const now = () => poller.refresh();
    window.addEventListener(INBOX_EVENT, now);
    window.addEventListener(WORKSPACE_EVENT, now);
    return () => {
      window.removeEventListener(INBOX_EVENT, now);
      window.removeEventListener(WORKSPACE_EVENT, now);
      poller.stop();
    };
  }, []);
  return { counts, announcement };
}

/** Picks the workspace to work in. Switching reloads the page so every screen reads the chosen workspace; the server re-checks membership on every request. */
export function WorkspaceSwitcher() {
  const [items, setItems] = useState<WorkspaceInfo[] | null>(null);
  const [current, setCurrent] = useState<string>("");
  useEffect(() => {
    let live = true;
    getApi()
      .listWorkspaces()
      .then((r) => {
        if (!live) return;
        setItems(r.items);
        const chosen = activeWorkspace();
        const known = r.items.find((w) => w.id === chosen);
        if (chosen && !known) selectWorkspace(null); // a stale choice (no longer a member): back to the personal workspace
        setCurrent(known ? known.id : (r.items.find((w) => w.personal)?.id ?? ""));
      })
      .catch(() => live && setItems([]));
    return () => {
      live = false;
    };
  }, []);
  if (!items) return <div className="st-ws st-skel" aria-hidden="true" style={{ height: "3rem" }} />;
  const here = items.find((w) => w.id === current) ?? items[0];
  if (items.length < 2) {
    // one workspace: nothing to choose between, but the person still sees WHERE they are and in what capacity
    return (
      <div className="st-ws" role="group" aria-label="Current workspace">
        <div className="st-ws-name">{here ? (here.personal ? "Personal" : here.name) : "Workspace"}</div>
        <div className="st-ws-role st-muted">{here ? `${here.role} · ${here.members} member${here.members === 1 ? "" : "s"}` : ""}</div>
      </div>
    );
  }
  return (
    <div className="st-field st-ws">
      <label className="st-label" htmlFor="workspace-switcher">
        Workspace
      </label>
      <select
        id="workspace-switcher"
        className="st-input"
        value={current}
        onChange={(e) => {
          const next = items.find((w) => w.id === e.target.value);
          selectWorkspace(next && !next.personal ? next.id : null);
          window.location.reload();
        }}
      >
        {items.map((w) => (
          <option key={w.id} value={w.id}>
            {w.personal ? "Personal" : w.name} ({w.role})
          </option>
        ))}
      </select>
    </div>
  );
}

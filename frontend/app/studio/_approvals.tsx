"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { ApiError } from "@/lib/api-client";
import { getApi } from "@/lib/backend";
import { actionSummary, actorLabel, approvalError, argumentRows, countdown, eventDetail, EVENT_TEXT, isLifecycleEvent, isOpen, phaseOf, secondsLeft } from "@/lib/studio/approvals";
import { createPoller } from "@/lib/studio/poller";
import type { ApprovalItem, RunEvent } from "@/lib/studio/types";
import { relativeTime } from "@/lib/studio/view";
import { Badge, ErrorNotice, Notice } from "./_ui";
import { refreshInbox } from "./_team";
import { explainError, type Explained } from "@/lib/studio/view";

/** A ticking clock (1 s) for countdowns only: it never touches the network. */
export function useNow(active = true): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [active]);
  return now;
}

/** ONE poller for a single approval: stops once it is no longer pending (the decision, an expiry or an invalidation is final). */
export function useApproval(id: string) {
  const [item, setItem] = useState<ApprovalItem | null>(null);
  const [error, setError] = useState<Explained | null>(null);
  const [loading, setLoading] = useState(true);
  const poller = useRef<ReturnType<typeof createPoller<ApprovalItem>> | null>(null);
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    const instance = createPoller<ApprovalItem>({
      fetch: () => getApi().getApproval(id),
      onData: (value) => {
        setItem(value);
        setError(null);
        setLoading(false);
      },
      onError: (e) => {
        setError(explainError(e, "Can't load this request"));
        setLoading(false);
      },
      isTerminal: (value) => !isOpen(value.status),
      intervalMs: 3000,
    });
    poller.current = instance;
    instance.start();
    return () => instance.stop();
  }, [id]);
  return { item, error, loading, setItem, refresh: () => poller.current?.refresh() };
}

export function ApprovalStatusBadge({ item }: { item: ApprovalItem }) {
  const meta = phaseOf(item);
  return (
    <Badge tone={meta.tone} pulse={item.status === "pending"}>
      {meta.label}
    </Badge>
  );
}

/** The decision: approve THIS exact action, or reject it. There is no blanket option, no "trust this agent" and no remembered choice. */
export function DecisionPanel({ item, onDecided }: { item: ApprovalItem; onDecided: (next: ApprovalItem) => void }) {
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const now = useNow(isOpen(item.status));
  const left = secondsLeft(item.expires_at, now);

  async function decide(kind: "approve" | "reject") {
    setBusy(kind);
    setProblem(null);
    try {
      onDecided(await getApi().decideApproval(item.id, kind, item.payload_hash));
      refreshInbox(); // the badge falls now, not at the next poll
    } catch (e) {
      setProblem(e instanceof ApiError ? approvalError(e.detail?.code) : approvalError(undefined));
    } finally {
      setBusy(null);
    }
  }

  const meta = phaseOf(item);
  if (!isOpen(item.status)) {
    return (
      <div className="st-decision" data-state={item.status} role="status" aria-live="polite">
        <div className="st-seal" data-tone={meta.tone} aria-hidden="true">
          {meta.seal}
        </div>
        <div>
          <div className="st-h2">{meta.label}</div>
          <p className="st-muted m-0 mt-1 text-sm">{meta.explain}</p>
          {item.decided_at ? <p className="st-muted m-0 mt-1 text-xs">Recorded {relativeTime(item.decided_at)}.</p> : null}
        </div>
      </div>
    );
  }
  const expired = left <= 0;
  if (!item.can_decide) {
    return (
      <div className="st-decision" data-state="pending">
        <div>
          <div className="st-h2">{meta.label}</div>
          <p className="st-muted m-0 mt-1 max-w-prose text-sm">{meta.explain}</p>
        </div>
        <Notice tone="warn" title="You can see this request, but you cannot decide it">
          <span className="text-sm">Only a workspace owner or admin can approve or reject an agent&apos;s action. Ask one of them to open this request.</span>
        </Notice>
        <p className="st-mono m-0 text-xs">{countdown(left)}</p>
      </div>
    );
  }
  return (
    <div className="st-decision" data-state="pending">
      <div>
        <div className="st-h2">Decide on this exact action</div>
        <p className="st-muted m-0 mt-1 max-w-prose text-sm">
          Approving lets the agent run <strong>this one call</strong>, with exactly the values above, once. It does not trust the agent with anything else, and the next call will ask again.
          Nothing has run, and nothing will until you decide.
        </p>
        <p className="st-mono m-0 mt-2 text-xs" aria-live="off" data-expired={expired || undefined}>
          {countdown(left)}
        </p>
      </div>
      {problem ? <Notice tone="bad" title="Couldn't record that decision">{problem}</Notice> : null}
      <div className="flex flex-wrap gap-3">
        <button className="st-btn st-btn-primary" onClick={() => void decide("approve")} disabled={busy !== null || expired}>
          {busy === "approve" ? "Approving…" : "Approve this exact action"}
        </button>
        <button className="st-btn" onClick={() => void decide("reject")} disabled={busy !== null || expired}>
          {busy === "reject" ? "Rejecting…" : "Reject this action"}
        </button>
      </div>
    </div>
  );
}

/** One request in the inbox: what, who, which task, when. */
export function ApprovalRow({ item, agentName, taskTitle, unread }: { item: ApprovalItem; agentName?: string; taskTitle?: string; unread?: boolean }) {
  const meta = phaseOf(item);
  const now = useNow(isOpen(item.status));
  return (
    <li>
      <Link href={`/studio/approvals/${item.id}`} className="st-appr-row" data-state={item.status}>
        <span className="st-appr-mark" data-tone={meta.tone} aria-hidden="true" />
        <span className="min-w-0">
          <span className="st-appr-what">
            {unread ? <span className="st-new">New</span> : null}
            {actionSummary(item)}
          </span>
          <span className="st-muted block truncate text-sm">
            {agentName ?? item.agent} asks to run <span className="st-mono">{item.tool.ref}</span>
            {taskTitle ? <> · for “{taskTitle}”</> : null}
            {!taskTitle && item.context?.task_title ? <> · for “{item.context.task_title}”</> : null}
            {item.context?.started_by === "another member" ? <> · started by another member</> : null}
          </span>
        </span>
        <span className="st-appr-side">
          <ApprovalStatusBadge item={item} />
          <span className="st-muted st-mono text-xs">{isOpen(item.status) ? countdown(secondsLeft(item.expires_at, now)) : relativeTime(item.decided_at ?? item.created_at)}</span>
        </span>
      </Link>
    </li>
  );
}

/** Under a task on the run page: what that task is waiting for, or what was decided. Real approvals only. */
export function TaskApprovals({ items }: { items: ApprovalItem[] }) {
  if (!items.length) return null;
  return (
    <ul className="st-task-appr" aria-label="Approvals for this task">
      {items.map((item) => {
        const meta = phaseOf(item);
        return (
          <li key={item.id} data-state={item.status}>
            <span className="st-appr-mark" data-tone={meta.tone} aria-hidden="true" />
            <span className="min-w-0 flex-1 text-sm">
              {isOpen(item.status) ? <strong>{item.suspended ? "Suspended: saved, waiting for your approval." : "Paused: waiting for your approval."}</strong> : <strong>{meta.label}.</strong>} <span className="st-muted">{actionSummary(item)}</span>
            </span>
            <Link href={`/studio/approvals/${item.id}`} className="st-btn st-btn-sm">
              {isOpen(item.status) ? "Review and decide" : "Open"}
            </Link>
          </li>
        );
      })}
    </ul>
  );
}

export function ArgumentTable({ args }: { args: ApprovalItem["arguments"] }) {
  const rows = argumentRows(args);
  if (!rows.length) return <p className="st-muted m-0 text-sm">No arguments.</p>;
  return (
    <dl className="st-args">
      {rows.map((row) => (
        <div key={row.name} className="st-args-row" data-long={row.long || undefined}>
          <dt className="st-mono">{row.name}</dt>
          <dd>{row.value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function LoadFailure({ error, onRetry }: { error: Explained; onRetry: () => void }) {
  return <ErrorNotice error={error} onRetry={onRetry} fallbackTitle="Couldn't load this request" />;
}

/** The run's history, from the real append-only event log: what happened, in order, and who did it. The approval and suspension lines are highlighted. */
export function RunHistory({ events }: { events: RunEvent[] }) {
  const [all, setAll] = useState(false);
  const shown = all ? events : events.filter((e) => isLifecycleEvent(e.type));
  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-2 pt-3">
        <p className="st-muted m-0 text-sm">{all ? "Every recorded event." : "Approvals, suspensions and recovery. This history shows what happened, including the recorded actions and their details. Sensitive values are redacted where recognized, but redaction is not guaranteed to catch every one."}</p>
        <button className="st-btn st-btn-sm" onClick={() => setAll(!all)} aria-pressed={all}>
          {all ? "Show approvals only" : "Show every event"}
        </button>
      </div>
      {shown.length ? (
        <ol className="st-history" aria-label="Run history">
          {shown.map((e) => {
            const known = EVENT_TEXT[e.type];
            const detail = eventDetail(e.detail);
            return (
              <li key={e.id} data-tone={known?.tone ?? "muted"}>
                <span className="st-appr-mark" data-tone={known?.tone ?? "muted"} aria-hidden="true" />
                <span className="min-w-0">
                  <span className="block font-medium">{known?.label ?? e.type.replaceAll("_", " ")}</span>
                  <span className="st-muted block text-xs">
                    {actorLabel(e.actor)}
                    {e.task ? ` · task ${e.task}` : ""}
                    {detail ? ` · ${detail}` : ""}
                  </span>
                </span>
                <span className="st-muted st-mono text-xs">{relativeTime(e.created_at)}</span>
              </li>
            );
          })}
        </ol>
      ) : (
        <p className="st-muted m-0 py-4 text-sm">No approval or suspension has happened in this run.</p>
      )}
    </div>
  );
}

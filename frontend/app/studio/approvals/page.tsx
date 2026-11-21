"use client";

import { useEffect, useRef, useState } from "react";
import { getApi } from "@/lib/backend";
import { inboxOrder } from "@/lib/studio/approvals";
import { createPoller } from "@/lib/studio/poller";
import type { ApprovalList, NotificationItem } from "@/lib/studio/types";
import { explainError, type Explained } from "@/lib/studio/view";
import { ApprovalRow } from "../_approvals";
import { refreshInbox } from "../_team";
import { EmptyState, ErrorNotice, PageHead, Section, Skeleton } from "../_ui";

/** The approval inbox: what your agents are paused on, then what was decided. One poller; real data only. */
export default function ApprovalInboxPage() {
  const [data, setData] = useState<ApprovalList | null>(null);
  const [error, setError] = useState<Explained | null>(null);
  const [loading, setLoading] = useState(true);
  const poller = useRef<ReturnType<typeof createPoller<ApprovalList>> | null>(null);
  const [unread, setUnread] = useState<NotificationItem[]>([]);

  useEffect(() => {
    const instance = createPoller<ApprovalList>({
      fetch: () => getApi().listApprovals({ limit: 50 }),
      onData: (value) => {
        void getApi().listNotifications({ unread: true, limit: 100 }).then((n) => setUnread(n.items)).catch(() => undefined);
        setData(value);
        setError(null);
        setLoading(false);
      },
      onError: (e) => {
        setError(explainError(e, "Can't load approvals"));
        setLoading(false);
      },
      isTerminal: () => false,
      intervalMs: 3000,
    });
    poller.current = instance;
    instance.start();
    return () => instance.stop();
  }, []);

  const { waiting, history } = inboxOrder(data?.items ?? []);
  const unreadIds = new Set(unread.map((n) => n.approval_id));
  async function markAllRead() {
    await getApi().markAllNotificationsRead();
    setUnread([]);
    refreshInbox();
  }
  return (
    <div className="mx-auto max-w-5xl">
      <PageHead
        eyebrow="Human approval"
        title="Your agents are asking first"
        lede="When an agent reaches an action that could change something outside its own work, it stops and asks. Each request is for one exact call: the agent, the tool and its version, and the values. Nothing runs until you say so."
      />
      <div className="mt-6">
        {loading && !data ? <Skeleton lines={3} height="4rem" /> : null}
        {error && !data ? <ErrorNotice error={error} onRetry={() => poller.current?.refresh()} fallbackTitle="Couldn't load approvals" /> : null}
        {data ? (
          <>
            <Section title={waiting.length ? `Waiting for you · ${waiting.length}` : "Waiting for you"} aside={<>Oldest first. These agents are paused.{unread.length ? <> <button className="st-btn st-btn-sm" onClick={() => void markAllRead()}>Mark {unread.length} as read</button></> : null}</>}>
              {waiting.length ? (
                <ul className="st-appr-list">
                  {waiting.map((item) => (
                    <ApprovalRow key={item.id} item={item} unread={unreadIds.has(item.id)} />
                  ))}
                </ul>
              ) : (
                <EmptyState title="Nothing is waiting for you">
                  Agents ask here when they reach a sensitive action. Give an agent a tool that needs approval, then run a task that uses it.
                </EmptyState>
              )}
            </Section>
            <Section title="Decided" aside="Newest first. Every request stays on record.">
              {history.length ? (
                <ul className="st-appr-list">
                  {history.map((item) => (
                    <ApprovalRow key={item.id} item={item} />
                  ))}
                </ul>
              ) : (
                <p className="st-muted m-0 py-4 text-sm">No request has been decided yet.</p>
              )}
            </Section>
          </>
        ) : null}
      </div>
    </div>
  );
}

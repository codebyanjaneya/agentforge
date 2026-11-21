"use client";

import { useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { getApi } from "@/lib/backend";
import { INVOCATION_STATUS } from "@/lib/studio/tools";
import type { InvocationStatus, ToolInvocationItem } from "@/lib/studio/types";
import { explainError, type Explained } from "@/lib/studio/view";
import { InvocationLine } from "../../_tools";
import { BackLink, EmptyState, ErrorNotice, Field, PageHead, Skeleton, Spinner, useLoader } from "../../_ui";

const STATUSES = Object.keys(INVOCATION_STATUS) as InvocationStatus[];

function Activity() {
  const initialTool = useSearchParams().get("tool") ?? "";
  const [tool, setTool] = useState(initialTool);
  const [status, setStatus] = useState("");
  const catalog = useLoader(() => getApi().listTools(), []);
  const [items, setItems] = useState<ToolInvocationItem[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Explained | null>(null);
  const ticket = useRef(0);

  const load = useCallback(
    async (after: string | null) => {
      const mine = ++ticket.current;
      setLoading(true);
      setError(null);
      try {
        const page = await getApi().listToolInvocations({ toolId: tool || undefined, status: status || undefined, limit: 25, cursor: after });
        if (mine !== ticket.current) return;
        setItems((current) => (after ? [...current, ...page.items] : page.items));
        setCursor(page.next_cursor);
      } catch (e) {
        if (mine === ticket.current) setError(explainError(e));
      } finally {
        if (mine === ticket.current) setLoading(false);
      }
    },
    [tool, status],
  );

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load(null);
  }, [load]);

  const filtered = !!(tool || status);
  return (
    <div className="mx-auto max-w-5xl">
      <BackLink href="/studio/tools">Tools</BackLink>
      <div className="mt-3">
        <PageHead eyebrow="Tool activity" title="What your agents actually did" lede="Every tool call made on your behalf, newest first: what was asked, what came back, how long it took, and the refusals too. Nothing here is estimated." />
      </div>
      <div className="mt-5 grid gap-3 sm:grid-cols-2">
        <Field label="Tool" htmlFor="f-tool">
          <select id="f-tool" className="st-select" value={tool} onChange={(e) => setTool(e.target.value)}>
            <option value="">Every tool</option>
            {catalog.data?.items.map((t) => (
              <option key={t.id} value={t.id}>
                {t.title} ({t.latest.ref})
              </option>
            ))}
          </select>
        </Field>
        <Field label="Outcome" htmlFor="f-status">
          <select id="f-status" className="st-select" value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">Any outcome</option>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {INVOCATION_STATUS[s].label}
              </option>
            ))}
          </select>
        </Field>
      </div>
      <div className="mt-5">
        {loading && !items.length ? <Skeleton lines={4} height="3rem" /> : null}
        {error ? <ErrorNotice error={error} onRetry={() => void load(null)} fallbackTitle="Couldn't load the activity" /> : null}
        {!loading && !error && !items.length ? (
          <EmptyState title={filtered ? "No calls match" : "No tool has been used yet"}>
            {filtered ? "Try another tool or outcome." : "Give an agent a tool, run a task, and the calls it makes will be listed here with their inputs and results."}
          </EmptyState>
        ) : null}
        {items.length ? (
          <ul className="st-calls">
            {items.map((item) => (
              <InvocationLine key={item.id} item={item} showAgent showTask />
            ))}
          </ul>
        ) : null}
        {cursor ? (
          <div className="mt-4">
            <button className="st-btn" onClick={() => void load(cursor)} disabled={loading}>
              {loading ? "Loading…" : "Load older calls"}
            </button>
          </div>
        ) : null}
        {loading && items.length ? <Spinner label="Loading" /> : null}
      </div>
    </div>
  );
}

export default function ToolActivityPage() {
  return (
    <Suspense fallback={<Skeleton lines={3} />}>
      <Activity />
    </Suspense>
  );
}

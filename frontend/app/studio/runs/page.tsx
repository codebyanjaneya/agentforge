"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { getApi } from "@/lib/backend";
import type { PlanSummary, RunListItem } from "@/lib/studio/types";
import { RUN_STATUS, explainError, relativeTime, type Explained } from "@/lib/studio/view";
import { Badge, EmptyState, ErrorNotice, PageHead, Section, Skeleton, Spinner } from "../_ui";

type Row = RunListItem & { plan: PlanSummary };

/**
 * Execution history. The backend lists runs per plan (there is no global run list), so this page reads the runs of the
 * approved plans, a bounded number of requests at a time. Open a run for live status; this list is not polled.
 */
export default function RunsPage() {
  const [rows, setRows] = useState<Row[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [more, setMore] = useState(false);
  const [error, setError] = useState<Explained | null>(null);

  const loadPlans = useCallback(async (after: string | null) => {
    const page = await getApi().listPlans({ status: "approved", limit: 20, cursor: after });
    const out: Row[] = [];
    for (let i = 0; i < page.items.length; i += 4) {
      const batch = page.items.slice(i, i + 4);
      const results = await Promise.all(batch.map((plan) => getApi().listRuns(plan.id).then((r) => r.items.map((run) => ({ ...run, plan })))));
      out.push(...results.flat());
    }
    return { rows: out, next: page.next_cursor };
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await loadPlans(null);
      setRows(result.rows.sort((a, b) => b.created_at.localeCompare(a.created_at)));
      setCursor(result.next);
    } catch (e) {
      setError(explainError(e));
    } finally {
      setLoading(false);
    }
  }, [loadPlans]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load]);

  async function loadMore() {
    if (!cursor || more) return;
    setMore(true);
    try {
      const result = await loadPlans(cursor);
      setRows((prev) => [...prev, ...result.rows].sort((a, b) => b.created_at.localeCompare(a.created_at)));
      setCursor(result.next);
    } catch (e) {
      setError(explainError(e));
    } finally {
      setMore(false);
    }
  }

  const active = rows.filter((r) => r.status === "running");

  return (
    <div className="mx-auto max-w-4xl">
      <PageHead eyebrow="Runs" title="Execution history" lede="Every time an approved plan was run, with how it ended. Open a run for live status, task outputs and failures." actions={<button className="st-btn" onClick={() => void load()} disabled={loading}>Refresh</button>} />
      {loading ? (
        <Skeleton lines={4} />
      ) : error ? (
        <div className="mt-5"><ErrorNotice error={error} onRetry={() => void load()} /></div>
      ) : rows.length === 0 ? (
        <EmptyState title="Nothing has run yet" action={<Link className="st-btn st-btn-primary" href="/studio/plans">Go to plans</Link>}>
          Approve a plan and start it; its runs will appear here.
        </EmptyState>
      ) : (
        <>
          {active.length ? (
            <Section title={`In progress · ${active.length}`}>
              {active.map((r) => (
                <RunRow key={r.id} row={r} />
              ))}
            </Section>
          ) : null}
          <Section title="History">
            {rows.filter((r) => r.status !== "running").map((r) => (
              <RunRow key={r.id} row={r} />
            ))}
            {cursor ? (
              <div className="pt-3">
                <button className="st-btn" onClick={() => void loadMore()} disabled={more}>
                  {more ? <Spinner /> : null}Check older plans
                </button>
              </div>
            ) : null}
          </Section>
        </>
      )}
    </div>
  );
}

function RunRow({ row }: { row: Row }) {
  return (
    <Link href={`/studio/runs/${row.id}`} className="st-row" style={{ gridTemplateColumns: "minmax(0,1fr) auto" }}>
      <div className="min-w-0">
        <div className="st-row-title truncate">{row.plan.objective}</div>
        <div className="st-muted text-xs">
          Attempt {row.attempt} · revision {row.revision} · {relativeTime(row.created_at)} · {row.counts.succeeded}/{row.counts.total} tasks succeeded
          {row.counts.failed ? ` · ${row.counts.failed} failed` : ""}
          {row.counts.skipped ? ` · ${row.counts.skipped} skipped` : ""}
        </div>
      </div>
      <Badge tone={RUN_STATUS[row.status].tone} pulse={row.status === "running"}>
        {RUN_STATUS[row.status].label}
      </Badge>
    </Link>
  );
}

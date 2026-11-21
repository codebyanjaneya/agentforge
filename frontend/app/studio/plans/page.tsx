"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { getApi } from "@/lib/backend";
import type { PlanListFilter, PlanSummary } from "@/lib/studio/types";
import { PLAN_STATUS, explainError, relativeTime, type Explained } from "@/lib/studio/view";
import { ObjectiveComposer } from "../_composer";
import { Badge, EmptyState, ErrorNotice, PageHead, Section, Skeleton, Spinner } from "../_ui";

const FILTERS: { value: PlanListFilter; label: string }[] = [
  { value: "active", label: "Active" },
  { value: "draft", label: "Awaiting approval" },
  { value: "approved", label: "Approved" },
  { value: "cancelled", label: "Cancelled" },
  { value: "all", label: "All" },
];

export default function PlansPage() {
  const [filter, setFilter] = useState<PlanListFilter>("active");
  const [items, setItems] = useState<PlanSummary[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [more, setMore] = useState(false);
  const [error, setError] = useState<Explained | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const page = await getApi().listPlans({ status: filter, limit: 20 });
      setItems(page.items);
      setCursor(page.next_cursor);
    } catch (e) {
      setError(explainError(e));
    } finally {
      setLoading(false);
    }
  }, [filter]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load]);

  // The cursor from the previous page is passed back unchanged; plans are keyed by id so a repeat is never shown twice.
  async function loadMore() {
    if (!cursor || more) return;
    setMore(true);
    try {
      const page = await getApi().listPlans({ status: filter, limit: 20, cursor });
      setItems((prev) => {
        const seen = new Set(prev.map((p) => p.id));
        return [...prev, ...page.items.filter((p) => !seen.has(p.id))];
      });
      setCursor(page.next_cursor);
    } catch (e) {
      setError(explainError(e));
    } finally {
      setMore(false);
    }
  }

  return (
    <div className="mx-auto max-w-4xl">
      <PageHead eyebrow="Plans" title="Plans" lede="Every plan is a reviewable proposal. Approve one when you are happy with it, then start it as a separate step." />
      <div className="mt-6">
        <ObjectiveComposer compact />
      </div>
      <Section
        title="Your plans"
        aside={
          <div role="tablist" aria-label="Filter plans" className="flex flex-wrap gap-1">
            {FILTERS.map((f) => (
              <button key={f.value} role="tab" aria-selected={filter === f.value} className="st-btn st-btn-sm" style={filter === f.value ? { borderColor: "var(--st-ember)", color: "var(--st-ember-text)" } : undefined} onClick={() => setFilter(f.value)}>
                {f.label}
              </button>
            ))}
          </div>
        }
      >
        {loading ? (
          <Skeleton lines={4} />
        ) : error ? (
          <ErrorNotice error={error} onRetry={() => void load()} />
        ) : items.length === 0 ? (
          <EmptyState title={filter === "active" ? "No plans yet" : "Nothing here"}>
            {filter === "active" ? "Describe an objective above to create your first plan." : "No plans match this filter."}
          </EmptyState>
        ) : (
          <>
            {items.map((p) => (
              <Link key={p.id} href={`/studio/plans/${p.id}`} className="st-row" style={{ gridTemplateColumns: "minmax(0,1fr) auto" }}>
                <div className="min-w-0">
                  <div className="st-row-title">{p.objective}</div>
                  <div className="st-muted text-xs">
                    {p.task_count} task{p.task_count === 1 ? "" : "s"} · revision {p.latest_revision}
                    {p.approved_revision ? ` (approved: ${p.approved_revision})` : ""}{p.created_by ? ` · by ${p.created_by}` : ""} · updated {relativeTime(p.updated_at)}
                  </div>
                </div>
                <Badge tone={PLAN_STATUS[p.status].tone}>{PLAN_STATUS[p.status].short}</Badge>
              </Link>
            ))}
            {cursor ? (
              <div className="pt-3">
                <button className="st-btn" onClick={() => void loadMore()} disabled={more}>
                  {more ? <Spinner /> : null}Load more
                </button>
              </div>
            ) : null}
          </>
        )}
      </Section>
    </div>
  );
}

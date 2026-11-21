"use client";

import Link from "next/link";
import { useState } from "react";
import { getApi } from "@/lib/backend";
import { BUILTIN_AGENTS } from "@/lib/studio/forms";
import { relativeTime } from "@/lib/studio/view";
import { Badge, EmptyState, ErrorNotice, PageHead, Section, Skeleton, useLoader } from "../_ui";

type Filter = "active" | "archived" | "all";

export default function AgentsPage() {
  const [filter, setFilter] = useState<Filter>("active");
  const agents = useLoader(() => getApi().listAgents(filter), [filter]);
  return (
    <div className="mx-auto max-w-4xl">
      <PageHead
        eyebrow="Agents"
        title="Your workforce"
        lede="Built-in specialists are always available. Custom agents carry your own instructions, are versioned, and are pinned to an exact version whenever a plan uses them."
        actions={
          <>
            <Link href="/studio/templates" className="st-btn">
              From a template
            </Link>
            <Link href="/studio/agents/new" className="st-btn st-btn-primary">
              New agent
            </Link>
          </>
        }
      />

      <Section title="Built-in agents" aside="Part of AgentForge; not editable">
        <div className="st-grid-2 pt-4">
          {BUILTIN_AGENTS.map((a) => (
            <div key={a.key} className="grid gap-1 border-b border-[var(--st-rule)] pb-3">
              <div className="flex items-center gap-2">
                <span className="st-row-title">{a.name}</span>
                <span className="st-chip">built-in</span>
              </div>
              <span className="st-muted text-sm">{a.blurb}</span>
            </div>
          ))}
        </div>
      </Section>

      <Section
        title="Custom agents"
        aside={
          <div role="tablist" aria-label="Filter agents" className="flex gap-1">
            {(["active", "archived", "all"] as Filter[]).map((f) => (
              <button key={f} role="tab" aria-selected={filter === f} className="st-btn st-btn-sm" style={filter === f ? { borderColor: "var(--st-ember)", color: "var(--st-ember-text)" } : undefined} onClick={() => setFilter(f)}>
                {f[0].toUpperCase() + f.slice(1)}
              </button>
            ))}
          </div>
        }
      >
        {agents.loading ? (
          <Skeleton lines={3} />
        ) : agents.error ? (
          <ErrorNotice error={agents.error} onRetry={agents.reload} />
        ) : agents.data!.items.length === 0 ? (
          <EmptyState
            title={filter === "archived" ? "No archived agents" : "No custom agents yet"}
            action={
              filter === "archived" ? undefined : (
                <>
                  <Link href="/studio/templates" className="st-btn">
                    Browse templates
                  </Link>
                  <Link href="/studio/agents/new" className="st-btn st-btn-primary">
                    Create from scratch
                  </Link>
                </>
              )
            }
          >
            {filter === "archived" ? "Archived agents appear here and can be restored." : "Create a specialist that knows your product, tone and process, then assign it to plan tasks."}
          </EmptyState>
        ) : (
          agents.data!.items.map((a) => (
            <Link key={a.id} href={`/studio/agents/${a.id}`} className="st-row" style={{ gridTemplateColumns: "minmax(0,1fr) auto" }}>
              <div className="min-w-0">
                <div className="flex flex-wrap items-baseline gap-2">
                  <span className="st-row-title">{a.name}</span>
                  <span className="st-muted text-sm">{a.role}</span>
                </div>
                <div className="st-muted text-xs">
                  <span className="st-mono">{a.slug}</span> · updated {relativeTime(a.updated_at)}
                  {a.template_id ? ` · from template ${a.template_id}` : ""}
                </div>
              </div>
              <div className="flex items-center gap-2">
                {a.status === "archived" ? <Badge tone="muted">Archived</Badge> : null}
                <span className="st-chip st-mono">v{a.current_version}</span>
              </div>
            </Link>
          ))
        )}
      </Section>
    </div>
  );
}

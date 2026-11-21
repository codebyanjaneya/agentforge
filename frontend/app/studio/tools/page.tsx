"use client";

import Link from "next/link";
import { getApi } from "@/lib/backend";
import { riskLabel } from "@/lib/studio/tools";
import type { ToolCatalogItem } from "@/lib/studio/types";
import { Lamp, RiskTag } from "../_tools";
import { EmptyState, ErrorNotice, Notice, PageHead, Skeleton, useLoader } from "../_ui";

function lastUsed(value: string | null): string {
  if (!value) return "never used";
  const minutes = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 60000));
  return minutes < 1 ? "used just now" : minutes < 60 ? `used ${minutes} min ago` : minutes < 1440 ? `used ${Math.round(minutes / 60)} h ago` : `used ${Math.round(minutes / 1440)} d ago`;
}

function Row({ tool }: { tool: ToolCatalogItem }) {
  const live = tool.latest.executable;
  return (
    <Link href={`/studio/tools/${tool.id}`} className="st-rack-row">
      <Lamp tone={live ? "good" : "muted"} label={live ? "Available to agents" : "Not available"} />
      <div className="min-w-0">
        <div className="st-rack-name">{tool.title}</div>
        <div className="st-rack-ref st-mono">{tool.latest.ref}</div>
      </div>
      <div className="min-w-0">
        <p className="m-0 text-sm">{tool.summary}</p>
        <div className="st-task-meta mt-1">
          <RiskTag tool={tool.latest} />
          {tool.latest.requires_approval && tool.latest.grantable ? <span className="st-chip" data-tone="warn">Asks you first</span> : live ? null : <span className="st-chip" data-tone="bad">{tool.latest.lifecycle === "disabled" ? "Switched off" : "Needs approval · unavailable"}</span>}
          {tool.capabilities.slice(0, 3).map((c) => (
            <span key={c} className="st-chip">
              {c}
            </span>
          ))}
          {tool.versions.length > 1 ? <span className="st-chip">{tool.versions.length} versions</span> : null}
        </div>
      </div>
      <div className="st-readout" aria-label={`${tool.usage.calls} calls, ${lastUsed(tool.usage.last_used)}`}>
        <b>{tool.usage.calls}</b>
        <span>{tool.usage.calls === 1 ? "call" : "calls"}</span>
        <span style={{ textTransform: "none", letterSpacing: 0 }}>{tool.agents.length ? `${tool.agents.length} of your agent${tool.agents.length === 1 ? "" : "s"}` : "no agent yet"}</span>
      </div>
    </Link>
  );
}

export default function ToolsPage() {
  const { data, error, loading, reload } = useLoader(() => getApi().listTools(), []);
  const groups = new Map<string, ToolCatalogItem[]>();
  for (const tool of data?.items ?? []) groups.set(tool.category, [...(groups.get(tool.category) ?? []), tool]);
  const total = data?.items.reduce((n, t) => n + t.usage.calls, 0) ?? 0;
  return (
    <div className="mx-auto max-w-5xl">
      <PageHead
        eyebrow="Instruments"
        title="Tools your agents can use"
        lede="Agents do more than write: with a tool they calculate, measure and look things up exactly. Each tool below is trusted code with fixed inputs and outputs. An agent can use only the tools you give it."
        actions={
          <Link href="/studio/tools/activity" className="st-btn">
            Tool activity{total ? ` · ${total}` : ""}
          </Link>
        }
      />
      <div className="mt-5">
        <Notice tone="info" title="What can run today">
          Read-only tools compute an answer and change nothing, so they run on their own. A tool that could change something outside AgentForge is marked “Asks you first”: the agent pauses at every call and you approve that exact call, or you do not.
        </Notice>
      </div>
      {loading && !data ? <div className="mt-6"><Skeleton lines={5} height="4.4rem" /></div> : null}
      {error && !data ? <div className="mt-6"><ErrorNotice error={error} onRetry={reload} fallbackTitle="Couldn't load the tools" /></div> : null}
      {data && !data.items.length ? <div className="mt-6"><EmptyState title="No tools are registered">Nothing can be given to an agent yet.</EmptyState></div> : null}
      {[...groups.entries()].map(([category, tools]) => (
        <section key={category} className="st-rack-group" aria-label={category}>
          <div className="st-eyebrow">{category}</div>
          <div className="st-rack" style={{ marginTop: 0 }}>
            {tools.map((tool) => (
              <Row key={tool.id} tool={tool} />
            ))}
          </div>
        </section>
      ))}
      {data ? (
        <p className="st-muted mt-8 text-sm">
          Give a tool to an agent in the agent editor ({riskLabel("read", "none").label.toLowerCase()} tools can be added by anyone who can edit the agent). The exact version you pick is saved with the agent, so
          a later release of a tool never changes what an approved agent can do.
        </p>
      ) : null}
    </div>
  );
}

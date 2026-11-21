"use client";

// Tool presentation (M4-B). Everything shown comes from the backend (registry specs, stored invocations); nothing is estimated. Inputs and outputs of
// a call are the stored, redacted records, rendered as labelled values, not raw JSON dumps.

import { dispatchFacts } from "@/lib/studio/dispatch";
import Link from "next/link";
import { useState } from "react";
import { getApi } from "@/lib/backend";
import { INVOCATION_STATUS, MAX_TOOLS_PER_AGENT, brief, durationLabel, explainInvocation, isSelected, portsOf, riskLabel, toggleTool, type Port } from "@/lib/studio/tools";
import type { JsonSchema, ToolInvocationDetail, ToolInvocationItem, ToolSummary } from "@/lib/studio/types";
import { explainError, type Explained } from "@/lib/studio/view";
import { Badge, EmptyState, ErrorNotice, Skeleton, Spinner, useLoader } from "./_ui";

export function Lamp({ tone, label }: { tone: "good" | "active" | "bad" | "warn" | "idle" | "muted"; label?: string }) {
  return <span className="st-lamp" data-tone={tone} role={label ? "img" : undefined} aria-label={label} aria-hidden={label ? undefined : true} />;
}

export function RiskTag({ tool }: { tool: Pick<ToolSummary, "risk" | "side_effect"> }) {
  const meta = riskLabel(tool.risk, tool.side_effect);
  return (
    <span className="st-chip" data-tone={meta.tone} title={meta.detail}>
      {meta.label}
    </span>
  );
}

// ------------------------------------------------------------------------------------------------ ports (schemas in words)

function PortRows({ ports, nested = false }: { ports: Port[]; nested?: boolean }) {
  return (
    <ul className={nested ? "st-port-list st-port-nested" : "st-port-list"}>
      {ports.map((port) => (
        <li key={port.name} className="st-port">
          <div className="flex flex-wrap items-baseline gap-x-2">
            <span className="st-mono st-port-name">{port.name}</span>
            <span className="st-port-type">{port.type}</span>
            <span className="st-muted text-xs">{port.required ? "required" : "optional"}</span>
          </div>
          {port.description ? <p className="st-muted m-0 text-sm">{port.description}</p> : null}
          {port.constraints.length ? <p className="st-port-limits">{port.constraints.join(" · ")}</p> : null}
          {port.fields.length ? <PortRows ports={port.fields} nested /> : null}
        </li>
      ))}
    </ul>
  );
}

export function PortPanel({ kind, schema }: { kind: "input" | "output"; schema: JsonSchema }) {
  const ports = portsOf(schema);
  return (
    <section className="st-ports" data-kind={kind} aria-label={kind === "input" ? "What the tool needs" : "What the tool returns"}>
      <div className="st-eyebrow">{kind === "input" ? "In — what it needs" : "Out — what it returns"}</div>
      {ports.length ? <PortRows ports={ports} /> : <p className="st-muted text-sm">No fields.</p>}
    </section>
  );
}

// ------------------------------------------------------------------------------------------------ values

export function ValueView({ value, depth = 0 }: { value: unknown; depth?: number }) {
  if (value === null || value === undefined) return <span className="st-muted">—</span>;
  if (typeof value !== "object") return <span className="st-value">{String(value)}</span>;
  if (depth >= 3) return <span className="st-value st-mono">{brief(value, 120)}</span>;
  if (Array.isArray(value)) {
    if (!value.length) return <span className="st-muted">empty list</span>;
    return (
      <ol className="st-values">
        {value.slice(0, 12).map((item, index) => (
          <li key={index}>
            <ValueView value={item} depth={depth + 1} />
          </li>
        ))}
        {value.length > 12 ? <li className="st-muted">… {value.length - 12} more</li> : null}
      </ol>
    );
  }
  return (
    <dl className="st-kv">
      {Object.entries(value as Record<string, unknown>).map(([key, v]) => (
        <div key={key} className="st-kv-row">
          <dt className="st-mono">{key}</dt>
          <dd>
            <ValueView value={v} depth={depth + 1} />
          </dd>
        </div>
      ))}
    </dl>
  );
}

// ------------------------------------------------------------------------------------------------ one tool call

export function InvocationLine({ item, showAgent = false, showTask = false }: { item: ToolInvocationItem; showAgent?: boolean; showTask?: boolean }) {
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<ToolInvocationDetail | null>(null);
  const [error, setError] = useState<Explained | null>(null);
  const [loading, setLoading] = useState(false);
  const meta = INVOCATION_STATUS[item.status];
  const why = explainInvocation(item.status, item.error);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      setDetail(await getApi().getToolInvocation(item.id));
    } catch (e) {
      setError(explainError(e));
    } finally {
      setLoading(false);
    }
  }

  function toggle() {
    const next = !open;
    setOpen(next);
    if (next && (!detail || item.status === "running")) void load();
  }

  return (
    <li className="st-call" data-tone={meta.tone}>
      <button className="st-call-head" onClick={toggle} aria-expanded={open}>
        <Lamp tone={meta.tone} />
        <span className="st-mono st-call-tool">
          {item.tool.id}
          {item.tool.version ? <span className="st-muted">@{item.tool.version}</span> : null}
        </span>
        <span className="st-call-preview">{item.input_preview ? brief(safeParse(item.input_preview), 80) : "—"}</span>
        <span className="st-call-end">
          {item.output_preview ? <span className="st-call-out">→ {brief(safeParse(item.output_preview), 40)}</span> : null}
          <Badge tone={meta.tone} pulse={item.status === "running"}>
            <span aria-hidden="true">{meta.glyph} </span>
            {meta.label}
          </Badge>
          {item.dispatch ? (
            <Badge tone={dispatchFacts(item.dispatch).tone} pulse={item.dispatch.state === "dispatching"}>
              {dispatchFacts(item.dispatch).label}
            </Badge>
          ) : null}
          {item.duration_ms !== null ? <span className="st-chip st-mono">{durationLabel(item.duration_ms)}</span> : null}
        </span>
      </button>
      {open ? (
        <div className="st-call-body">
          <div className="st-task-meta">
            {showAgent ? <span className="st-chip">agent {item.agent}</span> : null}
            {showTask && item.run_id && item.task ? (
              <Link className="st-chip" href={`/studio/runs/${item.run_id}#task-${item.task}`}>
                task {item.task} · open run
              </Link>
            ) : null}
            <span className="st-chip">call #{item.seq}</span>
            <span className="st-chip st-mono">{new Date(item.started_at).toLocaleString()}</span>
          </div>
          {why ? <p className="m-0 mt-2 text-sm" style={{ color: item.status === "succeeded" ? undefined : "var(--st-ink-2)" }}>{why}</p> : null}
          {loading ? <Spinner label="Loading the call" /> : null}
          {error ? <ErrorNotice error={error} onRetry={() => void load()} /> : null}
          {detail ? (
            <div className="st-call-io">
              <div>
                <div className="st-eyebrow">Input</div>
                {detail.input ? <ValueView value={detail.input} /> : <span className="st-muted text-sm">Nothing was recorded (the request was malformed).</span>}
              </div>
              <div>
                <div className="st-eyebrow">Output</div>
                {detail.output ? <ValueView value={detail.output} /> : <span className="st-muted text-sm">No result.</span>}
              </div>
            </div>
          ) : null}
          {detail?.args_hash ? (
            <p className="st-muted m-0 mt-2 text-xs">
              Recorded and redacted by the server. Fingerprint <span className="st-mono">{detail.args_hash.slice(0, 12)}</span>
            </p>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}

function safeParse(text: string): unknown {
  try {
    const parsed: unknown = JSON.parse(text);
    return parsed;
  } catch {
    return text; // a preview that was cut short is shown as text
  }
}

/** The calls one task made, drawn as a spur off the task on the execution rail. */
export function ToolTrail({ items }: { items: ToolInvocationItem[] }) {
  if (!items.length) return null;
  return (
    <div className="st-trail">
      <div className="st-eyebrow">Tools used · {items.length} call{items.length === 1 ? "" : "s"}</div>
      <ul className="st-calls">
        {items.map((item) => (
          <InvocationLine key={item.id} item={item} />
        ))}
      </ul>
    </div>
  );
}

// ------------------------------------------------------------------------------------------------ the agent editor's picker

export function ToolPicker({ selected, onChange, disabled }: { selected: string[]; onChange: (next: string[]) => void; disabled?: boolean }) {
  const { data, error, loading, reload } = useLoader(() => getApi().listTools(), []);
  if (loading && !data) return <Skeleton lines={3} height="2.6rem" />;
  if (error && !data) return <ErrorNotice error={error} onRetry={reload} fallbackTitle="Couldn't load the tools" />;
  const items = data?.items ?? [];
  if (!items.length) return <EmptyState title="No tools are registered">Agents will answer in writing only.</EmptyState>;
  const full = selected.length >= MAX_TOOLS_PER_AGENT;
  return (
    <div>
      <p className="st-muted m-0 mb-3 text-sm">
        Choose what this agent may use. It can use <strong>only</strong> these tools, at the exact version shown when you save. Every call is checked and recorded by the
        server. Read-only tools run on their own. A tool marked “Asks you first” pauses the agent at every call until you approve that exact call.
      </p>
      <ul className="st-picks">
        {items.map((tool) => {
          const on = isSelected(selected, tool.id);
          const blocked = !(tool.latest.executable || tool.latest.grantable);
          return (
            <li key={tool.id}>
              <label className="st-pick" data-on={on || undefined} data-blocked={blocked || undefined}>
                <input type="checkbox" checked={on} disabled={disabled || blocked || (!on && full)} onChange={() => onChange(toggleTool(selected, tool.id))} />
                <span className="st-pick-body">
                  <span className="flex flex-wrap items-baseline gap-x-2">
                    <span className="st-pick-title">{tool.title}</span>
                    <span className="st-mono st-muted text-xs">{tool.latest.ref}</span>
                  </span>
                  <span className="st-muted text-sm">{tool.summary}</span>
                </span>
                <RiskTag tool={tool.latest} />
                {tool.latest.requires_approval && tool.latest.grantable ? <span className="st-chip" data-tone="warn" title="Every call pauses the agent and asks you first">Asks you first</span> : null}
              </label>
            </li>
          );
        })}
      </ul>
      <p className="st-muted m-0 mt-2 text-xs">
        {selected.length} of {MAX_TOOLS_PER_AGENT} selected · <Link href="/studio/tools">See what each tool can and cannot do</Link>
      </p>
    </div>
  );
}

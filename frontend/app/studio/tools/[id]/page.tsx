"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import { getApi } from "@/lib/backend";
import { riskLabel } from "@/lib/studio/tools";
import { InvocationLine, Lamp, PortPanel } from "../../_tools";
import { BackLink, Badge, EmptyState, ErrorNotice, Notice, PageHead, Section, Skeleton, useLoader } from "../../_ui";

export default function ToolDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { data, error, loading, reload } = useLoader(() => getApi().getTool(id), [id]);
  const [picked, setPicked] = useState<number | null>(null);

  if (loading && !data) return <div className="mx-auto max-w-4xl"><Skeleton lines={5} height="4rem" /></div>;
  if (!data) {
    return (
      <div className="mx-auto max-w-4xl">
        <BackLink href="/studio/tools">Tools</BackLink>
        <div className="mt-4">{error ? <ErrorNotice error={error} onRetry={reload} fallbackTitle="Couldn't load this tool" /> : null}</div>
      </div>
    );
  }
  const versions = [...data.versions].sort((a, b) => b.version - a.version);
  const latest = versions.find((v) => v.lifecycle === "active") ?? versions[0];
  const version = versions.find((v) => v.version === (picked ?? latest.version)) ?? latest;
  const risk = riskLabel(version.risk, version.side_effect);
  return (
    <div className="mx-auto max-w-4xl">
      <BackLink href="/studio/tools">Tools</BackLink>
      <div className="mt-3">
        <PageHead
          eyebrow={`${version.category} · instrument`}
          title={version.title}
          lede={version.summary}
          actions={
            <>
              <Lamp tone={version.executable ? "good" : "muted"} />
              <Badge tone={version.executable ? "good" : version.requires_approval && version.grantable ? "warn" : "muted"}>{version.executable ? "Available to agents" : version.lifecycle === "disabled" ? "Switched off" : version.requires_approval && version.grantable ? "Asks you first" : "Needs approval"}</Badge>
            </>
          }
        />
      </div>
      <p className="st-mono mt-3 text-sm">
        {version.ref}
        {version.lifecycle === "deprecated" ? <span className="st-muted"> · deprecated</span> : null}
      </p>
      {versions.length > 1 ? (
        <div className="st-versions mt-3" role="group" aria-label="Versions">
          {versions.map((v) => (
            <button key={v.version} className="st-version" aria-pressed={v.version === version.version} onClick={() => setPicked(v.version)}>
              v{v.version}
              {v.lifecycle !== "active" ? ` · ${v.lifecycle}` : ""}
            </button>
          ))}
        </div>
      ) : null}

      <p className="mt-5 max-w-prose text-[0.98rem] leading-relaxed">{version.description}</p>

      <dl className="st-spec" aria-label="Limits and safety">
        <div>
          <dt>Safety</dt>
          <dd title={risk.detail}>{risk.label}</dd>
        </div>
        <div>
          <dt>Time limit</dt>
          <dd>{version.timeout_seconds} s</dd>
        </div>
        <div>
          <dt>Result size</dt>
          <dd>{(version.max_output_bytes / 1000).toLocaleString("en-US")} KB</dd>
        </div>
        <div>
          <dt>Used so far</dt>
          <dd>{version.usage.calls === 1 ? "1 call" : `${version.usage.calls} calls`}</dd>
        </div>
      </dl>
      <p className="st-muted mt-2 text-sm">{risk.detail}</p>

      {version.delivery ? (
        <Section title="Delivery guarantee" aside="What happens if the answer is lost.">
          <p className="mt-3 max-w-prose text-sm">{version.delivery.guarantee}</p>
          <dl className="st-spec" aria-label="Delivery facts">
            <div>
              <dt>Automatic retry</dt>
              <dd>{version.delivery.retry_policy === "after_confirmed_failure" ? "Only after a confirmed failure" : "Never"}</dd>
            </div>
            <div>
              <dt>Remote de-duplication</dt>
              <dd>{version.delivery.remote_idempotency ? "The service honours our idempotency key" : "None: AgentForge dispatches at most once"}</dd>
            </div>
            <div>
              <dt>Can ask the service</dt>
              <dd>{version.delivery.can_query_remote ? "Yes, to settle an unknown outcome" : "No: a person reconciles"}</dd>
            </div>
          </dl>
        </Section>
      ) : null}

      <div className="st-ports-pair">
        <PortPanel kind="input" schema={version.input_schema} />
        <PortPanel kind="output" schema={version.output_schema} />
      </div>

      <Section title="What it cannot do" aside="Honest limits of this version.">
        <ul className="st-cannot">
          {version.limitations.map((limit) => (
            <li key={limit}>{limit}</li>
          ))}
        </ul>
      </Section>

      <Section title="An example input" aside="A real, valid request.">
        <div className="pt-3">
          <dl className="st-kv">
            {Object.entries(version.example).map(([key, value]) => (
              <div key={key} className="st-kv-row">
                <dt className="st-mono">{key}</dt>
                <dd>{typeof value === "string" ? value : JSON.stringify(value)}</dd>
              </div>
            ))}
          </dl>
        </div>
      </Section>

      <Section title="Who can use it" aside="Your agents whose current version was given this tool.">
        <div className="pt-3">
          {data.agents.length ? (
            <ul className="m-0 grid list-none gap-2 p-0">
              {data.agents.map((agent) => (
                <li key={agent.id} className="flex flex-wrap items-center gap-2">
                  <Link href={`/studio/agents/${agent.id}`} className="font-medium">
                    {agent.name}
                  </Link>
                  <span className="st-chip st-mono">{agent.slug}</span>
                  {agent.version ? <span className="st-chip st-mono">uses v{agent.version}</span> : null}
                </li>
              ))}
            </ul>
          ) : (
            <EmptyState title="No agent has this tool yet" action={<Link href="/studio/agents" className="st-btn">Choose an agent</Link>}>
              Open an agent, pick this tool in its editor, and save: the agent can then use it in any task.
            </EmptyState>
          )}
        </div>
      </Section>

      <Section title="Recent calls" aside="Real calls made by your agents, newest first.">
        <div className="pt-3">
          {data.recent_invocations.length ? (
            <>
              <ul className="st-calls">
                {data.recent_invocations.map((item) => (
                  <InvocationLine key={item.id} item={item} showAgent showTask />
                ))}
              </ul>
              <p className="mt-3 text-sm">
                <Link href={`/studio/tools/activity?tool=${data.id}`}>All calls of this tool →</Link>
              </p>
            </>
          ) : (
            <Notice tone="info" title="Not used yet">
              When an agent uses this tool in a task, the call (what it asked, what came back, how long it took) will appear here.
            </Notice>
          )}
        </div>
      </Section>
    </div>
  );
}

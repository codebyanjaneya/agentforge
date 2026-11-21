"use client";

import { costLabel, tokens } from "@/lib/studio/usage";
import type { RunUsage, UsageSummary } from "@/lib/studio/types";

function Row({ label, sub, s }: { label: string; sub?: string; s: UsageSummary }) {
  const cost = costLabel(s);
  return (
    <tr>
      <th scope="row" className="text-left font-medium">
        {label}
        {sub ? <span className="st-muted block text-xs font-normal">{sub}</span> : null}
      </th>
      <td className="st-mono text-right">{s.requests}</td>
      <td className="st-mono text-right">{s.requests_without_usage === s.requests && s.requests > 0 ? "not reported" : tokens(s.input_tokens)}</td>
      <td className="st-mono text-right">{s.requests_without_usage === s.requests && s.requests > 0 ? "not reported" : tokens(s.output_tokens)}</td>
      <td className="st-mono text-right">
        {cost.text}
        {cost.note ? <span className="st-muted block text-xs font-sans">{cost.note}</span> : null}
      </td>
    </tr>
  );
}

/** Real, persisted usage of one run: tokens and cost per model and per task. A cost is shown only when it could be calculated; otherwise the table says why not. */
export function UsagePanel({ usage }: { usage: RunUsage }) {
  const t = usage.totals;
  if (t.requests === 0) return <p className="st-muted m-0 py-4 text-sm">No model request was recorded for this run.</p>;
  const total = costLabel(t);
  return (
    <div>
      <p className="m-0 pt-3 text-sm" data-testid="usage-total">
        <strong>{tokens(t.input_tokens)}</strong> input and <strong>{tokens(t.output_tokens)}</strong> output tokens in <strong>{t.requests}</strong> model request{t.requests === 1 ? "" : "s"}
        {t.cached_tokens ? <> ({tokens(t.cached_tokens)} cached)</> : null}. Cost: <strong>{total.text}</strong>
        {t.cost_complete ? <span className="st-muted"> — calculated</span> : null}.
      </p>
      {total.note ? <p className="st-muted m-0 mt-1 text-xs">{total.note}</p> : null}
      <div className="st-table-wrap mt-3" tabIndex={0} role="region" aria-label="Model usage by model">
        <table className="st-table w-full text-sm">
          <caption className="sr-only">Model usage and cost of this run</caption>
          <thead>
            <tr>
              <th scope="col" className="text-left">By model</th>
              <th scope="col" className="text-right">Requests</th>
              <th scope="col" className="text-right">Input</th>
              <th scope="col" className="text-right">Output</th>
              <th scope="col" className="text-right">Cost</th>
            </tr>
          </thead>
          <tbody>
            {usage.by_model.map((m) => (
              <Row key={`${m.provider}/${m.model}`} label={m.model} sub={m.provider} s={m} />
            ))}
          </tbody>
        </table>
      </div>
      {usage.by_task.length ? (
        <div className="st-table-wrap mt-3" tabIndex={0} role="region" aria-label="Model usage by task">
          <table className="st-table w-full text-sm">
            <caption className="sr-only">Model usage and cost by task</caption>
            <thead>
              <tr>
                <th scope="col" className="text-left">By task</th>
                <th scope="col" className="text-right">Requests</th>
                <th scope="col" className="text-right">Input</th>
                <th scope="col" className="text-right">Output</th>
                <th scope="col" className="text-right">Cost</th>
              </tr>
            </thead>
            <tbody>
              {usage.planning ? <Row label="Planning" sub="the model that wrote the plan" s={usage.planning} /> : null}
              {usage.by_task.map((task) => (
                <Row key={task.task ?? "?"} label={task.task ?? "task"} sub={task.agent ?? undefined} s={task} />
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      <p className="st-muted m-0 mt-2 text-xs">
        Tokens are what the provider reported. Costs use AgentForge&apos;s configured prices (version {usage.pricing_versions.join(", ") || usage.pricing.current_version}), not an invoice. {usage.pricing.note}
      </p>
    </div>
  );
}

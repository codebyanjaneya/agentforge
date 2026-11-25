"use client";

import Link from "next/link";
import { useState } from "react";
import { getApi } from "@/lib/backend";
import type { NextAction } from "@/lib/studio/home";
import { CONTROL_LINES } from "@/lib/studio/home";
import type { StarterAgent } from "@/lib/studio/types";
import { explainError, type Explained } from "@/lib/studio/view";
import { ErrorNotice, Spinner, useLoader } from "./_ui";

/** A slim, honest banner: in demo mode the model is a script and the sending tool is a simulation. Everything else on screen is real. */
export function DemoBanner() {
  const demo = useLoader(() => getApi().getDemo(), []);
  if (!demo.data) return null;
  if (!demo.data.demo) {
    return (
      <div className="st-live" role="note" aria-label="Live mode">
        <strong className="st-mono">LIVE MODEL</strong>
        <span>
          Plans and agent work are written by <span className="st-mono">{demo.data.model_name ?? "the configured model"}</span>. Real lookups: <span className="st-mono">{demo.data.live_tools.join(", ")}</span>.
        </span>
      </div>
    );
  }
  return (
    <div className="st-demo" role="note" aria-label="Demo mode">
      <strong className="st-mono">DEMO MODE</strong>
      <span>
        The model is a <strong>script</strong>, not a live model, and <span className="st-mono">{demo.data.simulated_tools.join(", ")}</span> is a <strong>simulation</strong> that sends nothing.
        The plan, the tool results, the approvals and the history are real.
      </span>
    </div>
  );
}

/** The one thing to do next, from real data. */
export function NextStep({ action, onHere, busy = false }: { action: NextAction; onHere: () => void; busy?: boolean }) {
  return (
    <section className="st-next st-rise" data-kind={action.kind} aria-label="Next step">
      <div className="min-w-0">
        <div className="st-eyebrow">Next</div>
        <h2 className="st-h2 mt-1">{action.title}</h2>
        <p className="st-muted m-0 mt-1 max-w-prose text-sm">{action.detail}</p>
      </div>
      {action.href ? (
        <Link href={action.href} className="st-btn st-btn-primary">
          {action.cta}
        </Link>
      ) : (
        <button type="button" className="st-btn st-btn-primary" onClick={onHere} disabled={busy}>
          {busy ? "Adding…" : action.cta}
        </button>
      )}
    </section>
  );
}

export function ControlStrip() {
  return (
    <ul className="st-control" aria-label="How control works">
      {CONTROL_LINES.map((line) => (
        <li key={line.name}>
          <span className="st-eyebrow">{line.name}</span>
          <span className="st-muted text-sm leading-relaxed">{line.text}</span>
        </li>
      ))}
    </ul>
  );
}

const STARTER_NOTES: Record<string, string> = {
  "unit-economics-analyst": "Works out whether a price holds up: LTV, LTV:CAC and payback, computed exactly.",
  "launch-planner": "Turns “in about two weeks” into a real date by counting business days.",
  "launch-comms": "Drafts the customer note, checks it reads easily and asks you before anything is sent.",
};

/** Creating the starter workforce: ONE hook, used by every button that offers it, so the action exists in exactly one place and only ever runs from an explicit click. */
export function useStarterWorkforce(onCreated: () => void) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Explained | null>(null);
  const [made, setMade] = useState<StarterAgent[] | null>(null);
  async function add() {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      setMade((await getApi().createStarterWorkforce()).agents);
      onCreated();
    } catch (e) {
      setError(explainError(e, "Couldn't add the starter workforce"));
    } finally {
      setBusy(false);
    }
  }
  return { busy, error, made, add };
}

/** What the starter workforce is, and (once created) the exact tools each specialist may use, as the backend returned them. */
export function StarterWorkforce({ state, compact = false }: { state: ReturnType<typeof useStarterWorkforce>; compact?: boolean }) {
  const { busy, error, made, add } = state;
  const templates = useLoader(() => getApi().listTemplates(), []);
  const names = ["unit-economics-analyst", "launch-planner", "launch-comms"];
  const shown = templates.data?.items.filter((t) => names.includes(t.id)).sort((a, b) => names.indexOf(a.id) - names.indexOf(b.id)) ?? [];

  return (
    <div className="st-starter" id="starter">
      {compact ? <div className="st-eyebrow">What the starter workforce includes</div> : null}
      <p className="st-muted m-0 text-sm leading-relaxed" hidden={compact}>
        An agent is a specialist: written instructions, the tools it is allowed to use, and a model. A workforce is a set of them. You describe an objective, the planner assigns the work, and you approve the plan. These three are ready to use; you can also create your own.
      </p>
      <ol className="st-starter-list">
        {(made ?? shown.map((t) => ({ slug: t.id, name: t.name, role: t.role, tools: [], created: false }))).map((a) => (
          <li key={a.slug}>
            <div className="font-medium">{a.name}</div>
            <div className="st-muted text-sm">{STARTER_NOTES[a.slug] ?? a.role}</div>
            {a.tools.length ? (
              <div className="st-task-meta mt-1">
                {a.tools.map((t) => (
                  <span key={t} className="st-chip st-mono">
                    {t}
                  </span>
                ))}
              </div>
            ) : null}
          </li>
        ))}
      </ol>
      {error ? <ErrorNotice error={error} /> : null}
      {made || compact ? null : (
        <div className="st-step-actions" style={{ marginTop: 0 }}>
          <button type="button" className="st-btn st-btn-primary" onClick={() => void add()} disabled={busy || !shown.length}>
            {busy ? <Spinner /> : null}
            {busy ? "Adding…" : "Add the starter workforce"}
          </button>
          <Link href="/studio/agents/new" className="st-btn">
            Create your own agent
          </Link>
        </div>
      )}
    </div>
  );
}

"use client";

import { Check } from "lucide-react";
import Link from "next/link";
import { useId } from "react";
import type { SetupStep } from "@/lib/studio/onboarding";

const STATE_WORD = { done: "Done", current: "Next", todo: "Later" } as const;

/**
 * The first-run checklist: four steps from "nothing" to "I read what my agents did". It is derived from real workspace facts (see lib/studio/onboarding.ts) and
 * every action here already exists elsewhere in the Studio: it only puts the right one in front of a new person. The agent explanation lives in step 1.
 */
export function SetupChecklist({
  steps,
  done,
  onAssemble,
  assembling,
  onWrite,
  planHref,
  runHref,
}: {
  steps: SetupStep[];
  done: number;
  onAssemble: () => void;
  assembling: boolean;
  onWrite: () => void;
  planHref: string | null;
  runHref: string | null;
}) {
  const headingId = useId();
  return (
    <section className="st-setup st-rise" aria-labelledby={headingId}>
      <div className="st-setup-head">
        <div>
          <div className="st-eyebrow">Get started</div>
          <h2 className="st-h2 mt-1" id={headingId}>
            Your first result in four steps
          </h2>
        </div>
        <span className="st-hint">
          {done} of {steps.length} done, read from your workspace
        </span>
      </div>
      <ol className="st-steps">
        {steps.map((step, index) => (
          <li key={step.id} className="st-step" data-state={step.state} aria-current={step.state === "current" ? "step" : undefined}>
            <span className="st-step-node" aria-hidden="true">
              {step.state === "done" ? <Check size={15} strokeWidth={2.5} /> : index + 1}
            </span>
            <div className="min-w-0">
              <h3>
                {step.title}
                <span className="st-step-state">{STATE_WORD[step.state]}</span>
              </h3>
              {step.state !== "done" ? <p>{step.detail}</p> : null}
              {step.state === "current" ? <StepActions id={step.id} onAssemble={onAssemble} assembling={assembling} onWrite={onWrite} planHref={planHref} runHref={runHref} /> : null}
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}

function StepActions({ id, onAssemble, assembling, onWrite, planHref, runHref }: { id: SetupStep["id"]; onAssemble: () => void; assembling: boolean; onWrite: () => void; planHref: string | null; runHref: string | null }) {
  if (id === "workforce") {
    return (
      <div className="st-step-actions">
        <button type="button" className="st-btn st-btn-primary" onClick={onAssemble} disabled={assembling}>
          {assembling ? "Adding…" : "Add the starter workforce"}
        </button>
        <Link href="/studio/agents/new" className="st-btn">
          Create your own agent
        </Link>
        <Link href="/studio/templates" className="st-btn">
          Start from a template
        </Link>
      </div>
    );
  }
  if (id === "objective") {
    return (
      <div className="st-step-actions">
        <button type="button" className="st-btn st-btn-primary" onClick={onWrite}>
          Write an objective
        </button>
      </div>
    );
  }
  if (id === "approve") {
    return planHref ? (
      <div className="st-step-actions">
        <Link href={planHref} className="st-btn st-btn-primary">
          Review the plan
        </Link>
      </div>
    ) : null;
  }
  const href = runHref ?? planHref;
  return href ? (
    <div className="st-step-actions">
      <Link href={href} className="st-btn st-btn-primary">
        {runHref ? "Open the run" : "Open the plan"}
      </Link>
    </div>
  ) : null;
}

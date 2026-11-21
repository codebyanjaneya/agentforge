"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { getApi } from "@/lib/backend";
import { createPlanFlow } from "@/lib/studio/flows";
import { explainError, type Explained } from "@/lib/studio/view";
import { ErrorNotice, Spinner } from "./_ui";

const STARTERS = [
  "Launch our new product to the first 50 customers within a month.",
  "Prepare a hiring plan for our first two engineers.",
  "Analyse our top three competitors and recommend how to position against them.",
];

/**
 * The primary delegation entry. It only GENERATES a plan: nothing runs, and the user is taken to the plan to read it.
 * While the planner works we show the real elapsed time, never an invented percentage.
 */
export function ObjectiveComposer({ compact = false, examples }: { compact?: boolean; examples?: string[] }) {
  const router = useRouter();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [error, setError] = useState<Explained | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  useEffect(() => {
    if (!busy) return;
    const started = Date.now();
    const timer = setInterval(() => setElapsed(Math.floor((Date.now() - started) / 1000)), 1000);
    return () => clearInterval(timer);
  }, [busy]);

  const length = text.trim().length;
  const valid = length >= 10 && length <= 2000;

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!valid || busy) return;
    setBusy(true);
    setElapsed(0);
    setError(null);
    try {
      const plan = await createPlanFlow(getApi(), text);
      router.push(`/studio/plans/${plan.id}`);
    } catch (e) {
      if (mounted.current) setError(explainError(e, "Couldn't create the plan"));
    } finally {
      if (mounted.current) setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="st-panel st-rise grid gap-3 p-4 sm:p-5" aria-describedby="objective-help">
      <label htmlFor="objective" className="st-eyebrow">
        Your objective
      </label>
      <textarea
        id="objective"
        className="st-textarea"
        style={{ minHeight: compact ? "5rem" : "7rem", fontSize: "1.02rem" }}
        placeholder="What should your AI workforce get done? Describe the outcome you want."
        value={text}
        maxLength={2000}
        onChange={(e) => setText(e.target.value)}
        disabled={busy}
        aria-invalid={text.length > 0 && !valid}
      />
      <div id="objective-help" className="flex flex-wrap items-center justify-between gap-3">
        <div className="st-hint">
          AgentForge will propose a plan for you to review. <strong>Nothing runs until you approve it and start it.</strong>
        </div>
        <span className="st-mono st-hint">{length}/2000</span>
      </div>
      {!busy && length === 0 ? (
        <div className="flex flex-wrap gap-2" aria-label="Example objectives">
          {(examples?.length ? examples : STARTERS).map((s) => (
            <button key={s} type="button" className="st-chip cursor-pointer text-left hover:border-[var(--st-ember)]" onClick={() => setText(s)}>
              {s}
            </button>
          ))}
        </div>
      ) : null}
      {error ? <ErrorNotice error={error} /> : null}
      <div className="flex flex-wrap items-center gap-3">
        <button className="st-btn st-btn-primary" type="submit" disabled={!valid || busy}>
          {busy ? <Spinner /> : null}
          {busy ? "Planning…" : "Generate plan"}
        </button>
        {busy ? (
          <span className="st-hint" role="status" aria-live="polite">
            The planner is reading your objective and choosing agents · <span className="st-mono">{elapsed}s</span>
          </span>
        ) : null}
      </div>
    </form>
  );
}

"use client";

import Link from "next/link";
import { useState } from "react";
import { useSession } from "next-auth/react";
import { describeError, type ImprovementReport } from "@/lib/api-client";
import { getApi } from "@/lib/backend";
import { ThemeToggle } from "../theme-toggle";

const AGENTS = [
  { key: "ceo", label: "CEO", icon: "🧠" },
  { key: "sales", label: "Sales", icon: "💼" },
  { key: "marketing", label: "Marketing", icon: "📣" },
  { key: "support", label: "Support", icon: "🎧" },
  { key: "research", label: "Research", icon: "🔬" },
  { key: "finance", label: "Finance", icon: "📊" },
  { key: "hiring", label: "Hiring", icon: "👨‍💻" },
] as const;

export default function ImprovePage() {
  const { status } = useSession();
  const signedIn = status === "authenticated";
  const [agent, setAgent] = useState<string>("sales");
  const [loading, setLoading] = useState(false);
  const [report, setReport] = useState<ImprovementReport | null>(null);
  const [error, setError] = useState("");

  const analyze = async () => {
    if (!signedIn) {
      setError("Sign in to use AgentForge.");
      return;
    }
    setLoading(true);
    setError("");
    setReport(null);
    try {
      setReport(await getApi().analyzeAgent(agent));
    } catch (e) {
      setError(describeError(e, "Analysis failed"));
    } finally {
      setLoading(false);
    }
  };

  const noHistory = report && report.has_history === false;

  return (
    <main className="min-h-screen w-full px-4 py-10 sm:px-8">
      <div className="mx-auto max-w-5xl">
        <header className="mb-10 flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <span className="mono text-[11px] uppercase tracking-[0.3em] text-accent">
              Reinforcement Loop
            </span>
            <h1 className="mt-2 font-display text-4xl font-bold tracking-tight text-ink sm:text-5xl">
              Self-Improving Agents
            </h1>
            <p className="mt-2 text-sm text-muted">
              Analyze past performance and evolve agent strategy.
            </p>
          </div>
          <div className="flex items-center gap-2.5">
            <ThemeToggle />
            <Link
              href="/"
              className="inline-flex w-fit items-center gap-2 rounded-xl border border-hairline bg-panel px-4 py-2 text-sm font-medium text-ink/80 backdrop-blur transition-colors hover:border-accent/50 hover:text-ink"
            >
              ← Back to Agents
            </Link>
          </div>
        </header>

        <section className="glass glass-hover mb-6 p-6">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
            <select
              value={agent}
              onChange={(e) => setAgent(e.target.value)}
              className="rounded-xl border border-hairline bg-field px-3 py-2 text-sm text-ink outline-none transition focus:border-accent/50 focus:ring-2 focus:ring-accent/30"
            >
              {AGENTS.map((a) => (
                <option key={a.key} value={a.key}>
                  {a.icon} {a.label}
                </option>
              ))}
            </select>
            <button
              onClick={analyze}
              disabled={loading || !signedIn}
              className="inline-flex items-center justify-center gap-2 rounded-xl bg-accent px-5 py-2.5 text-sm font-semibold text-white shadow-[0_10px_30px_-10px_rgba(99,102,241,0.8)] transition hover:bg-accent-soft disabled:cursor-not-allowed disabled:opacity-40"
            >
              {loading ? (
                <>
                  <span className="h-4 w-4 animate-spin rounded-full border-2 border-white/30 border-t-white" />
                  Analyzing…
                </>
              ) : !signedIn ? (
                "Sign in to use AgentForge"
              ) : (
                "Analyze & Improve"
              )}
            </button>
          </div>
        </section>

        {error && (
          <p className="mb-6 rounded-xl border border-rose-500/30 bg-rose-500/10 p-3 text-sm text-rose-300">
            {error}
          </p>
        )}

        {noHistory && (
          <p className="glass p-6 text-center text-muted">
            Run some tasks first to enable self-improvement analysis
          </p>
        )}

        {report && !noHistory && (
          <div className="flex flex-col gap-6">
            <Section
              title="✅ Strengths"
              items={report.strengths}
              accent="text-online"
              empty="No strengths identified yet."
            />
            <Section
              title="⚠️ Weaknesses"
              items={report.weaknesses}
              accent="text-warn"
              empty="No weaknesses identified yet."
            />
            <Section
              title="🚀 Improvements"
              items={report.improvements}
              accent="text-accent-soft"
              empty="No improvements suggested yet."
            />

            <section className="glass p-6">
              <h2 className="mb-3 font-display text-lg font-semibold text-ink">
                📋 Updated Strategy
              </h2>
              <p className="whitespace-pre-wrap text-sm leading-relaxed text-ink/90">
                {report.updated_strategy || "No updated strategy provided."}
              </p>
            </section>
          </div>
        )}
      </div>
    </main>
  );
}

function Section({
  title,
  items,
  accent,
  empty,
}: {
  title: string;
  items: string[];
  accent: string;
  empty: string;
}) {
  return (
    <section className="glass p-6">
      <h2 className="mb-4 font-display text-lg font-semibold text-ink">
        {title}
      </h2>
      {items.length === 0 ? (
        <p className="text-sm text-muted/70">{empty}</p>
      ) : (
        <div className="flex flex-col gap-3">
          {items.map((item, index) => (
            <div
              key={index}
              className={`rounded-xl border border-hairline bg-panel p-3 text-sm ${accent}`}
            >
              {item}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

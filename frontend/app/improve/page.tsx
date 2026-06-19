"use client";

import Link from "next/link";
import { useState } from "react";

const API_BASE = "http://localhost:8000";

const AGENTS = [
  { key: "ceo", label: "CEO", icon: "🧠" },
  { key: "sales", label: "Sales", icon: "💼" },
  { key: "marketing", label: "Marketing", icon: "📣" },
  { key: "support", label: "Support", icon: "🎧" },
  { key: "research", label: "Research", icon: "🔬" },
  { key: "finance", label: "Finance", icon: "📊" },
  { key: "hiring", label: "Hiring", icon: "👨‍💻" },
] as const;

interface ImprovementReport {
  agent_name: string;
  strengths: string[];
  weaknesses: string[];
  improvements: string[];
  updated_strategy: string;
  has_history?: boolean;
  analyzed_at?: string;
}

export default function ImprovePage() {
  const [agent, setAgent] = useState<string>("sales");
  const [loading, setLoading] = useState(false);
  const [report, setReport] = useState<ImprovementReport | null>(null);
  const [error, setError] = useState("");

  const analyze = async () => {
    setLoading(true);
    setError("");
    setReport(null);
    try {
      const res = await fetch(`${API_BASE}/improve/${agent}`, {
        method: "POST",
      });
      if (!res.ok) throw new Error(`Request failed (${res.status})`);
      setReport(await res.json());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Analysis failed");
    } finally {
      setLoading(false);
    }
  };

  const noHistory = report && report.has_history === false;

  return (
    <main className="min-h-screen w-full bg-gray-950 px-4 py-10 sm:px-8">
      <div className="mx-auto max-w-5xl">
        <header className="mb-10 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <h1 className="text-4xl font-bold tracking-tight text-white">
              Self-Improving Agents
            </h1>
            <p className="mt-1 text-gray-400">
              Analyze past performance and evolve agent strategy
            </p>
          </div>
          <Link
            href="/"
            className="inline-flex w-fit items-center gap-2 rounded-lg border border-gray-700 bg-gray-900/60 px-4 py-2 text-sm font-medium text-gray-200 transition-colors hover:border-gray-500 hover:bg-gray-800"
          >
            ← Back to Agents
          </Link>
        </header>

        <section className="mb-6 rounded-2xl border border-gray-800 bg-gray-900/60 p-6 shadow-lg backdrop-blur">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
            <select
              value={agent}
              onChange={(e) => setAgent(e.target.value)}
              className="rounded-lg border border-gray-700 bg-gray-950/80 px-3 py-2 text-sm text-gray-100 outline-none focus:ring-2 focus:ring-blue-500/50"
            >
              {AGENTS.map((a) => (
                <option key={a.key} value={a.key}>
                  {a.icon} {a.label}
                </option>
              ))}
            </select>
            <button
              onClick={analyze}
              disabled={loading}
              className="inline-flex items-center justify-center gap-2 rounded-lg bg-blue-600 px-5 py-2.5 text-sm font-medium text-white transition-colors hover:bg-blue-500 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {loading ? (
                <>
                  <span className="h-4 w-4 animate-spin rounded-full border-2 border-white/30 border-t-white" />
                  Analyzing...
                </>
              ) : (
                "Analyze & Improve"
              )}
            </button>
          </div>
        </section>

        {error && (
          <p className="mb-6 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400">
            {error}
          </p>
        )}

        {noHistory && (
          <p className="rounded-2xl border border-gray-800 bg-gray-900/60 p-6 text-center text-gray-400">
            Run some tasks first to enable self-improvement analysis
          </p>
        )}

        {report && !noHistory && (
          <div className="flex flex-col gap-6">
            <Section
              title="✅ Strengths"
              items={report.strengths}
              accent="border-green-500/30 text-green-300"
              empty="No strengths identified yet."
            />
            <Section
              title="⚠️ Weaknesses"
              items={report.weaknesses}
              accent="border-amber-500/30 text-amber-300"
              empty="No weaknesses identified yet."
            />
            <Section
              title="🚀 Improvements"
              items={report.improvements}
              accent="border-blue-500/30 text-blue-300"
              empty="No improvements suggested yet."
            />

            <section className="rounded-2xl border border-purple-500/30 bg-gray-900/60 p-6 shadow-lg backdrop-blur">
              <h2 className="mb-3 text-lg font-semibold text-white">
                📋 Updated Strategy
              </h2>
              <p className="whitespace-pre-wrap text-sm text-gray-200">
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
    <section className="rounded-2xl border border-gray-800 bg-gray-900/60 p-6 shadow-lg backdrop-blur">
      <h2 className="mb-4 text-lg font-semibold text-white">{title}</h2>
      {items.length === 0 ? (
        <p className="text-sm text-gray-600">{empty}</p>
      ) : (
        <div className="flex flex-col gap-3">
          {items.map((item, index) => (
            <div
              key={index}
              className={`rounded-lg border bg-gray-950/60 p-3 text-sm text-gray-200 ${accent}`}
            >
              {item}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

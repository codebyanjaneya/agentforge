"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { ThemeToggle } from "../theme-toggle";

const API_BASE = "https://65-0-30-8.sslip.io";

const AGENTS = [
  { key: "ceo", label: "CEO", icon: "🧠" },
  { key: "sales", label: "Sales", icon: "💼" },
  { key: "marketing", label: "Marketing", icon: "📣" },
  { key: "support", label: "Support", icon: "🎧" },
  { key: "research", label: "Research", icon: "🔬" },
  { key: "finance", label: "Finance", icon: "📊" },
  { key: "hiring", label: "Hiring", icon: "👨‍💻" },
] as const;

const ACTION_META: Record<string, { icon: string; color: string }> = {
  email_draft: { icon: "✉️", color: "text-online border-online/30" },
  report_generated: { icon: "📄", color: "text-accent-soft border-accent/30" },
  strategy_created: { icon: "🎯", color: "text-online border-online/30" },
  leads_found: { icon: "🔍", color: "text-accent-soft border-accent/30" },
  analysis_done: { icon: "📈", color: "text-online border-online/30" },
};

interface ActionItem {
  action_type: string;
  description: string;
  status: string;
}

interface ExecutionResult {
  agent: string;
  task: string;
  response: string;
  actions: ActionItem[];
  executed_at: string;
}

interface ScheduledTask {
  id: string;
  agent: string;
  task: string;
  interval_minutes: number;
  created_at: string;
}

export default function AutonomousPage() {
  const [agent, setAgent] = useState<string>("sales");
  const [task, setTask] = useState("");
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<ExecutionResult | null>(null);
  const [error, setError] = useState("");

  const [schedAgent, setSchedAgent] = useState<string>("sales");
  const [schedTask, setSchedTask] = useState("");
  const [interval, setInterval] = useState(60);
  const [scheduled, setScheduled] = useState<ScheduledTask[]>([]);

  const loadTasks = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/tasks`);
      if (res.ok) setScheduled(await res.json());
    } catch {
      // backend offline; ignore
    }
  }, []);

  useEffect(() => {
    loadTasks();
  }, [loadTasks]);

  const execute = async () => {
    if (!task.trim()) return;
    setRunning(true);
    setError("");
    setResult(null);
    try {
      const res = await fetch(`${API_BASE}/autonomous/run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent, task }),
      });
      if (!res.ok) throw new Error(`Request failed (${res.status})`);
      setResult(await res.json());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Execution failed");
    } finally {
      setRunning(false);
    }
  };

  const schedule = async () => {
    if (!schedTask.trim()) return;
    try {
      const res = await fetch(`${API_BASE}/tasks/schedule`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          agent: schedAgent,
          task: schedTask,
          interval_minutes: interval,
        }),
      });
      if (res.ok) {
        setSchedTask("");
        await loadTasks();
      }
    } catch {
      // ignore
    }
  };

  const removeTask = async (id: string) => {
    try {
      const res = await fetch(`${API_BASE}/tasks/${id}`, { method: "DELETE" });
      if (res.ok) await loadTasks();
    } catch {
      // ignore
    }
  };

  return (
    <main className="min-h-screen w-full px-4 py-10 sm:px-8">
      <div className="mx-auto max-w-5xl">
        <header className="mb-10 flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <span className="mono text-[11px] uppercase tracking-[0.3em] text-accent">
              Execution Engine
            </span>
            <h1 className="mt-2 font-display text-4xl font-bold tracking-tight text-ink sm:text-5xl">
              Autonomous Execution
            </h1>
            <p className="mt-2 text-sm text-muted">
              Run agents and let them take concrete actions.
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
          <h2 className="mb-4 font-display text-lg font-semibold text-ink">
            Run a Task
          </h2>
          <div className="flex flex-col gap-4">
            <div className="flex flex-col gap-2 sm:flex-row">
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
              <textarea
                value={task}
                onChange={(e) => setTask(e.target.value)}
                placeholder="Describe the task to execute autonomously…"
                rows={2}
                className="flex-1 resize-none rounded-xl border border-hairline bg-field p-3 text-sm text-ink placeholder-muted/70 outline-none transition focus:border-accent/50 focus:ring-2 focus:ring-accent/30"
              />
            </div>
            <button
              onClick={execute}
              disabled={running || !task.trim()}
              className="inline-flex items-center justify-center gap-2 self-start rounded-xl bg-accent px-5 py-2.5 text-sm font-semibold text-white shadow-[0_10px_30px_-10px_rgba(99,102,241,0.8)] transition hover:bg-accent-soft disabled:cursor-not-allowed disabled:opacity-40"
            >
              {running ? (
                <>
                  <span className="h-4 w-4 animate-spin rounded-full border-2 border-white/30 border-t-white" />
                  Executing…
                </>
              ) : (
                "Execute"
              )}
            </button>
          </div>
        </section>

        {error && (
          <p className="mb-6 rounded-xl border border-rose-500/30 bg-rose-500/10 p-3 text-sm text-rose-300">
            {error}
          </p>
        )}

        {result && (
          <section className="glass mb-6 p-6">
            <div className="mb-4 flex items-center justify-between">
              <h2 className="font-display text-lg font-semibold text-ink">
                Response
              </h2>
              <span className="mono text-[11px] uppercase tracking-wider text-muted">
                {new Date(result.executed_at).toLocaleString()}
              </span>
            </div>
            <p className="mb-6 whitespace-pre-wrap text-sm leading-relaxed text-ink/90">
              {result.response}
            </p>

            <h3 className="mono mb-3 text-[10px] uppercase tracking-[0.2em] text-muted">
              Actions Taken
            </h3>
            <div className="flex flex-col gap-3">
              {result.actions.map((action, index) => {
                const meta =
                  ACTION_META[action.action_type] ?? {
                    icon: "⚙️",
                    color: "text-muted border-hairline",
                  };
                return (
                  <div
                    key={`${action.action_type}-${index}`}
                    className="flex items-start justify-between gap-3 rounded-xl border border-hairline bg-panel p-3"
                  >
                    <div className="flex items-start gap-3">
                      <span className="text-xl">{meta.icon}</span>
                      <div>
                        <span
                          className={`inline-block rounded-full border px-2 py-0.5 text-xs font-medium capitalize ${meta.color}`}
                        >
                          {action.action_type.replace(/_/g, " ")}
                        </span>
                        <p className="mt-1 text-sm text-ink/90">
                          {action.description}
                        </p>
                      </div>
                    </div>
                    <span className="mono flex-shrink-0 text-[11px] font-medium text-online">
                      ✓ done
                    </span>
                  </div>
                );
              })}
            </div>
          </section>
        )}

        <section className="glass p-6">
          <h2 className="mb-4 font-display text-lg font-semibold text-ink">
            Scheduled Recurring Tasks
          </h2>
          <div className="mb-5 flex flex-col gap-2 sm:flex-row">
            <select
              value={schedAgent}
              onChange={(e) => setSchedAgent(e.target.value)}
              className="rounded-xl border border-hairline bg-field px-3 py-2 text-sm text-ink outline-none transition focus:border-online/50 focus:ring-2 focus:ring-online/30"
            >
              {AGENTS.map((a) => (
                <option key={a.key} value={a.key}>
                  {a.icon} {a.label}
                </option>
              ))}
            </select>
            <input
              value={schedTask}
              onChange={(e) => setSchedTask(e.target.value)}
              placeholder="Recurring task…"
              className="flex-1 rounded-xl border border-hairline bg-field px-3 py-2 text-sm text-ink placeholder-muted/70 outline-none transition focus:border-online/50 focus:ring-2 focus:ring-online/30"
            />
            <div className="flex items-center gap-2">
              <input
                type="number"
                min={1}
                value={interval}
                onChange={(e) => setInterval(Number(e.target.value))}
                className="mono w-24 rounded-xl border border-hairline bg-field px-3 py-2 text-sm text-ink outline-none transition focus:border-online/50 focus:ring-2 focus:ring-online/30"
              />
              <span className="text-sm text-muted">min</span>
            </div>
            <button
              onClick={schedule}
              disabled={!schedTask.trim()}
              className="rounded-xl border border-online/40 bg-online/10 px-4 py-2 text-sm font-semibold text-online transition hover:bg-online/20 disabled:cursor-not-allowed disabled:opacity-40"
            >
              Schedule
            </button>
          </div>

          {scheduled.length === 0 ? (
            <p className="text-sm text-muted/70">No scheduled tasks yet.</p>
          ) : (
            <ul className="flex flex-col gap-3">
              {scheduled.map((t) => {
                const meta = AGENTS.find((a) => a.key === t.agent);
                return (
                  <li
                    key={t.id}
                    className="flex items-center justify-between gap-3 rounded-xl border border-hairline bg-panel p-3"
                  >
                    <div className="flex items-center gap-3">
                      <span className="text-lg">{meta?.icon ?? "⚙️"}</span>
                      <div>
                        <p className="text-sm text-ink/90">{t.task}</p>
                        <p className="mono text-[11px] uppercase tracking-wider text-muted">
                          {meta?.label ?? t.agent} · every {t.interval_minutes}{" "}
                          min
                        </p>
                      </div>
                    </div>
                    <button
                      onClick={() => removeTask(t.id)}
                      className="rounded-xl border border-rose-500/30 px-3 py-1.5 text-xs font-medium text-rose-300 transition-colors hover:bg-rose-500/10"
                    >
                      Delete
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      </div>
    </main>
  );
}

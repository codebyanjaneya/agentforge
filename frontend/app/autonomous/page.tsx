"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

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

const ACTION_META: Record<string, { icon: string; color: string }> = {
  email_draft: { icon: "✉️", color: "text-blue-400 border-blue-500/30" },
  report_generated: { icon: "📄", color: "text-emerald-400 border-emerald-500/30" },
  strategy_created: { icon: "🎯", color: "text-amber-400 border-amber-500/30" },
  leads_found: { icon: "🔍", color: "text-purple-400 border-purple-500/30" },
  analysis_done: { icon: "📈", color: "text-orange-400 border-orange-500/30" },
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
    <main className="min-h-screen w-full bg-gray-950 px-4 py-10 sm:px-8">
      <div className="mx-auto max-w-5xl">
        <header className="mb-10 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <h1 className="text-4xl font-bold tracking-tight text-white">
              Autonomous Execution
            </h1>
            <p className="mt-1 text-gray-400">
              Run agents and let them take concrete actions
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
          <h2 className="mb-4 text-lg font-semibold text-white">Run a Task</h2>
          <div className="flex flex-col gap-4">
            <div className="flex flex-col gap-2 sm:flex-row">
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
              <textarea
                value={task}
                onChange={(e) => setTask(e.target.value)}
                placeholder="Describe the task to execute autonomously..."
                rows={2}
                className="flex-1 resize-none rounded-lg border border-gray-700 bg-gray-950/80 p-3 text-sm text-gray-100 placeholder-gray-500 outline-none focus:ring-2 focus:ring-blue-500/50"
              />
            </div>
            <button
              onClick={execute}
              disabled={running || !task.trim()}
              className="inline-flex items-center justify-center gap-2 self-start rounded-lg bg-blue-600 px-5 py-2.5 text-sm font-medium text-white transition-colors hover:bg-blue-500 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {running ? (
                <>
                  <span className="h-4 w-4 animate-spin rounded-full border-2 border-white/30 border-t-white" />
                  Executing...
                </>
              ) : (
                "Execute"
              )}
            </button>
          </div>
        </section>

        {error && (
          <p className="mb-6 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400">
            {error}
          </p>
        )}

        {result && (
          <section className="mb-6 rounded-2xl border border-gray-800 bg-gray-900/60 p-6 shadow-lg backdrop-blur">
            <div className="mb-4 flex items-center justify-between">
              <h2 className="text-lg font-semibold text-white">Response</h2>
              <span className="text-xs text-gray-500">
                {new Date(result.executed_at).toLocaleString()}
              </span>
            </div>
            <p className="mb-6 whitespace-pre-wrap text-sm text-gray-200">
              {result.response}
            </p>

            <h3 className="mb-3 text-xs font-medium uppercase tracking-wide text-gray-500">
              Actions Taken
            </h3>
            <div className="flex flex-col gap-3">
              {result.actions.map((action, index) => {
                const meta =
                  ACTION_META[action.action_type] ?? {
                    icon: "⚙️",
                    color: "text-gray-300 border-gray-700",
                  };
                return (
                  <div
                    key={`${action.action_type}-${index}`}
                    className={`flex items-start justify-between gap-3 rounded-lg border bg-gray-950/60 p-3 ${meta.color}`}
                  >
                    <div className="flex items-start gap-3">
                      <span className="text-xl">{meta.icon}</span>
                      <div>
                        <span
                          className={`inline-block rounded-full border px-2 py-0.5 text-xs font-medium capitalize ${meta.color}`}
                        >
                          {action.action_type.replace(/_/g, " ")}
                        </span>
                        <p className="mt-1 text-sm text-gray-200">
                          {action.description}
                        </p>
                      </div>
                    </div>
                    <span className="flex-shrink-0 text-xs font-medium text-green-400">
                      ✓ Completed
                    </span>
                  </div>
                );
              })}
            </div>
          </section>
        )}

        <section className="rounded-2xl border border-gray-800 bg-gray-900/60 p-6 shadow-lg backdrop-blur">
          <h2 className="mb-4 text-lg font-semibold text-white">
            Scheduled Recurring Tasks
          </h2>
          <div className="mb-5 flex flex-col gap-2 sm:flex-row">
            <select
              value={schedAgent}
              onChange={(e) => setSchedAgent(e.target.value)}
              className="rounded-lg border border-gray-700 bg-gray-950/80 px-3 py-2 text-sm text-gray-100 outline-none focus:ring-2 focus:ring-emerald-500/50"
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
              placeholder="Recurring task..."
              className="flex-1 rounded-lg border border-gray-700 bg-gray-950/80 px-3 py-2 text-sm text-gray-100 placeholder-gray-500 outline-none focus:ring-2 focus:ring-emerald-500/50"
            />
            <div className="flex items-center gap-2">
              <input
                type="number"
                min={1}
                value={interval}
                onChange={(e) => setInterval(Number(e.target.value))}
                className="w-24 rounded-lg border border-gray-700 bg-gray-950/80 px-3 py-2 text-sm text-gray-100 outline-none focus:ring-2 focus:ring-emerald-500/50"
              />
              <span className="text-sm text-gray-500">min</span>
            </div>
            <button
              onClick={schedule}
              disabled={!schedTask.trim()}
              className="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-emerald-500 disabled:cursor-not-allowed disabled:opacity-50"
            >
              Schedule
            </button>
          </div>

          {scheduled.length === 0 ? (
            <p className="text-sm text-gray-600">No scheduled tasks yet.</p>
          ) : (
            <ul className="flex flex-col gap-3">
              {scheduled.map((t) => {
                const meta = AGENTS.find((a) => a.key === t.agent);
                return (
                  <li
                    key={t.id}
                    className="flex items-center justify-between gap-3 rounded-lg border border-gray-800 bg-gray-950/60 p-3"
                  >
                    <div className="flex items-center gap-3">
                      <span className="text-lg">{meta?.icon ?? "⚙️"}</span>
                      <div>
                        <p className="text-sm text-gray-200">{t.task}</p>
                        <p className="text-xs text-gray-500">
                          {meta?.label ?? t.agent} · every {t.interval_minutes}{" "}
                          min
                        </p>
                      </div>
                    </div>
                    <button
                      onClick={() => removeTask(t.id)}
                      className="rounded-lg border border-red-500/30 px-3 py-1.5 text-xs font-medium text-red-400 transition-colors hover:bg-red-500/10"
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

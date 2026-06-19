"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

type AgentKey =
  | "ceo"
  | "sales"
  | "marketing"
  | "support"
  | "research"
  | "finance"
  | "hiring";
type Status = "idle" | "running" | "done";

interface AgentConfig {
  key: AgentKey;
  name: string;
  icon: string;
  placeholder: string;
  accent: {
    ring: string;
    border: string;
    button: string;
    glow: string;
    text: string;
  };
}

const AGENTS: AgentConfig[] = [
  {
    key: "ceo",
    name: "CEO",
    icon: "🧠",
    placeholder: "e.g. I want 50 clients this month, create a plan...",
    accent: {
      ring: "focus:ring-amber-500/50",
      border: "border-amber-500/30 hover:border-amber-500/60",
      button: "bg-amber-600 hover:bg-amber-500",
      glow: "shadow-amber-500/10",
      text: "text-amber-400",
    },
  },
  {
    key: "sales",
    name: "Sales",
    icon: "💼",
    placeholder: "e.g. Find 10 qualified leads for a B2B SaaS product...",
    accent: {
      ring: "focus:ring-blue-500/50",
      border: "border-blue-500/30 hover:border-blue-500/60",
      button: "bg-blue-600 hover:bg-blue-500",
      glow: "shadow-blue-500/10",
      text: "text-blue-400",
    },
  },
  {
    key: "marketing",
    name: "Marketing",
    icon: "📣",
    placeholder: "e.g. Draft a launch campaign for our new feature...",
    accent: {
      ring: "focus:ring-purple-500/50",
      border: "border-purple-500/30 hover:border-purple-500/60",
      button: "bg-purple-600 hover:bg-purple-500",
      glow: "shadow-purple-500/10",
      text: "text-purple-400",
    },
  },
  {
    key: "support",
    name: "Support",
    icon: "🎧",
    placeholder: "e.g. Write an empathetic reply to a refund request...",
    accent: {
      ring: "focus:ring-green-500/50",
      border: "border-green-500/30 hover:border-green-500/60",
      button: "bg-green-600 hover:bg-green-500",
      glow: "shadow-green-500/10",
      text: "text-green-400",
    },
  },
  {
    key: "research",
    name: "Research",
    icon: "🔬",
    placeholder: "e.g. Analyze the top 3 competitors in our space...",
    accent: {
      ring: "focus:ring-orange-500/50",
      border: "border-orange-500/30 hover:border-orange-500/60",
      button: "bg-orange-600 hover:bg-orange-500",
      glow: "shadow-orange-500/10",
      text: "text-orange-400",
    },
  },
  {
    key: "finance",
    name: "Finance",
    icon: "📊",
    placeholder: "e.g. Track our burn rate and predict next month cashflow...",
    accent: {
      ring: "focus:ring-emerald-500/50",
      border: "border-emerald-500/30 hover:border-emerald-500/60",
      button: "bg-emerald-600 hover:bg-emerald-500",
      glow: "shadow-emerald-500/10",
      text: "text-emerald-400",
    },
  },
  {
    key: "hiring",
    name: "Hiring",
    icon: "👨‍💻",
    placeholder: "e.g. Screen these 5 resumes for a React developer role...",
    accent: {
      ring: "focus:ring-pink-500/50",
      border: "border-pink-500/30 hover:border-pink-500/60",
      button: "bg-pink-600 hover:bg-pink-500",
      glow: "shadow-pink-500/10",
      text: "text-pink-400",
    },
  },
];

interface AgentState {
  task: string;
  status: Status;
  result: string;
  error: string;
  primaryResponse: string;
  delegations: Delegation[];
}

interface Delegation {
  agent: string;
  task: string;
  response: string;
}

const INITIAL_STATE: Record<AgentKey, AgentState> = {
  ceo: {
    task: "",
    status: "idle",
    result: "",
    error: "",
    primaryResponse: "",
    delegations: [],
  },
  sales: {
    task: "",
    status: "idle",
    result: "",
    error: "",
    primaryResponse: "",
    delegations: [],
  },
  marketing: {
    task: "",
    status: "idle",
    result: "",
    error: "",
    primaryResponse: "",
    delegations: [],
  },
  support: {
    task: "",
    status: "idle",
    result: "",
    error: "",
    primaryResponse: "",
    delegations: [],
  },
  research: {
    task: "",
    status: "idle",
    result: "",
    error: "",
    primaryResponse: "",
    delegations: [],
  },
  finance: {
    task: "",
    status: "idle",
    result: "",
    error: "",
    primaryResponse: "",
    delegations: [],
  },
  hiring: {
    task: "",
    status: "idle",
    result: "",
    error: "",
    primaryResponse: "",
    delegations: [],
  },
};

const STATUS_STYLES: Record<Status, string> = {
  idle: "bg-gray-700/50 text-gray-300",
  running: "bg-yellow-500/20 text-yellow-300 animate-pulse",
  done: "bg-green-500/20 text-green-300",
};

const WS_URL = "ws://localhost:8000/ws";

export default function Home() {
  const [agents, setAgents] =
    useState<Record<AgentKey, AgentState>>(INITIAL_STATE);
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  const updateAgent = useCallback(
    (key: AgentKey, patch: Partial<AgentState>) => {
      setAgents((prev) => ({ ...prev, [key]: { ...prev[key], ...patch } }));
    },
    []
  );

  useEffect(() => {
    let reconnectTimer: ReturnType<typeof setTimeout>;

    const connect = () => {
      const ws = new WebSocket(WS_URL);
      wsRef.current = ws;

      ws.onopen = () => setConnected(true);
      ws.onclose = () => {
        setConnected(false);
        reconnectTimer = setTimeout(connect, 2000);
      };
      ws.onerror = () => ws.close();
      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data) as {
            agent?: AgentKey;
            result?: string;
            error?: string;
            primary_response?: string;
            delegations?: Delegation[];
          };
          if (!data.agent) return;
          if (data.error) {
            updateAgent(data.agent, {
              status: "idle",
              error: data.error,
              result: "",
              primaryResponse: "",
              delegations: [],
            });
          } else {
            updateAgent(data.agent, {
              status: "done",
              result: data.result ?? "",
              error: "",
              primaryResponse: data.primary_response ?? "",
              delegations: data.delegations ?? [],
            });
          }
        } catch {
          // ignore malformed messages
        }
      };
    };

    connect();

    return () => {
      clearTimeout(reconnectTimer);
      wsRef.current?.close();
    };
  }, [updateAgent]);

  const runAgent = useCallback(
    (key: AgentKey) => {
      const state = agents[key];
      const task = state.task.trim();
      if (!task) return;

      const ws = wsRef.current;
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        updateAgent(key, {
          status: "idle",
          error: "Not connected to backend. Is the server running?",
        });
        return;
      }

      updateAgent(key, {
        status: "running",
        result: "",
        error: "",
        primaryResponse: "",
        delegations: [],
      });
      ws.send(JSON.stringify({ agent: key, task }));
    },
    [agents, updateAgent]
  );

  const connectionBadge = useMemo(
    () =>
      connected
        ? "bg-green-500/20 text-green-300 border-green-500/40"
        : "bg-red-500/20 text-red-300 border-red-500/40",
    [connected]
  );

  return (
    <main className="min-h-screen w-full bg-gray-950 px-4 py-10 sm:px-8">
      <div className="mx-auto max-w-7xl">
        <header className="mb-10 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <h1 className="text-4xl font-bold tracking-tight text-white">
              AgentForge
            </h1>
            <p className="mt-1 text-gray-400">Multi-Agent AI Operating System</p>
          </div>
          <div className="flex items-center gap-3">
            <Link
              href="/improve"
              className="inline-flex w-fit items-center gap-2 rounded-lg border border-gray-700 bg-gray-900/60 px-4 py-2 text-sm font-medium text-gray-200 transition-colors hover:border-gray-500 hover:bg-gray-800"
            >
              ⚡ Improve
            </Link>
            <Link
              href="/autonomous"
              className="inline-flex w-fit items-center gap-2 rounded-lg border border-gray-700 bg-gray-900/60 px-4 py-2 text-sm font-medium text-gray-200 transition-colors hover:border-gray-500 hover:bg-gray-800"
            >
              ⚡ Autonomous
            </Link>
            <Link
              href="/twin"
              className="inline-flex w-fit items-center gap-2 rounded-lg border border-gray-700 bg-gray-900/60 px-4 py-2 text-sm font-medium text-gray-200 transition-colors hover:border-gray-500 hover:bg-gray-800"
            >
              📊 Digital Twin
            </Link>
            <span
              className={`inline-flex w-fit items-center gap-2 rounded-full border px-3 py-1 text-xs font-medium ${connectionBadge}`}
            >
              <span
                className={`h-2 w-2 rounded-full ${
                  connected ? "bg-green-400" : "bg-red-400"
                }`}
              />
              {connected ? "Connected" : "Disconnected"}
            </span>
          </div>
        </header>

        <div className="grid grid-cols-1 gap-6 md:grid-cols-2 lg:grid-cols-3">
          {AGENTS.map((config) => {
            const state = agents[config.key];
            return (
              <section
                key={config.key}
                className={`flex flex-col rounded-2xl border bg-gray-900/60 p-6 shadow-lg backdrop-blur transition-colors ${config.accent.border} ${config.accent.glow}`}
              >
                <div className="mb-4 flex items-center justify-between">
                  <h2 className="flex items-center gap-2 text-xl font-semibold text-white">
                    <span className="text-2xl">{config.icon}</span>
                    <span className={config.accent.text}>{config.name}</span>
                    <span className="text-gray-500">Agent</span>
                  </h2>
                  <span
                    className={`rounded-full px-2.5 py-1 text-xs font-medium capitalize ${
                      STATUS_STYLES[state.status]
                    }`}
                  >
                    {state.status}
                  </span>
                </div>

                <textarea
                  value={state.task}
                  onChange={(e) =>
                    updateAgent(config.key, { task: e.target.value })
                  }
                  placeholder={config.placeholder}
                  rows={3}
                  className={`w-full resize-none rounded-lg border border-gray-700 bg-gray-950/80 p-3 text-sm text-gray-100 placeholder-gray-500 outline-none focus:ring-2 ${config.accent.ring}`}
                />

                <button
                  onClick={() => runAgent(config.key)}
                  disabled={state.status === "running" || !state.task.trim()}
                  className={`mt-3 inline-flex items-center justify-center gap-2 rounded-lg px-4 py-2.5 text-sm font-medium text-white transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${config.accent.button}`}
                >
                  {state.status === "running" ? (
                    <>
                      <span className="h-4 w-4 animate-spin rounded-full border-2 border-white/30 border-t-white" />
                      Running...
                    </>
                  ) : (
                    "Run Agent"
                  )}
                </button>

                <div className="mt-4 flex-1">
                  <p className="mb-2 text-xs font-medium uppercase tracking-wide text-gray-500">
                    Response
                  </p>
                  <div className="min-h-[120px] rounded-lg border border-gray-800 bg-gray-950/60 p-3 text-sm">
                    {state.status === "running" && (
                      <p className="text-gray-400">Thinking...</p>
                    )}
                    {state.error && (
                      <p className="text-red-400">{state.error}</p>
                    )}
                    {!state.error && state.result && (
                      <p className="whitespace-pre-wrap text-gray-200">
                        {state.delegations.length > 0
                          ? state.primaryResponse || state.result
                          : state.result}
                      </p>
                    )}
                    {state.status === "idle" &&
                      !state.result &&
                      !state.error && (
                        <p className="text-gray-600">
                          Run the agent to see its response here.
                        </p>
                      )}
                  </div>

                  {state.delegations.length > 0 && (
                    <div className="mt-5">
                      <p className="mb-3 text-xs font-medium uppercase tracking-wide text-gray-500">
                        Delegated Tasks
                      </p>
                      <div className="flex flex-col gap-3">
                        {state.delegations.map((delegation, index) => {
                          const sub = AGENTS.find(
                            (a) => a.key === delegation.agent
                          );
                          const accentText = sub?.accent.text ?? "text-gray-300";
                          const accentBorder =
                            sub?.accent.border ?? "border-gray-700";
                          return (
                            <div
                              key={`${delegation.agent}-${index}`}
                              className={`rounded-lg border bg-gray-900/60 p-3 ${accentBorder}`}
                            >
                              <div className="mb-1 flex items-center gap-2">
                                {sub && <span>{sub.icon}</span>}
                                <span
                                  className={`text-sm font-semibold capitalize ${accentText}`}
                                >
                                  {delegation.agent} Agent
                                </span>
                              </div>
                              <p className="mb-2 text-xs italic text-gray-500">
                                {delegation.task}
                              </p>
                              <p className="whitespace-pre-wrap text-sm text-gray-200">
                                {delegation.response}
                              </p>
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  )}
                </div>
              </section>
            );
          })}
        </div>
      </div>
    </main>
  );
}

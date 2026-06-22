"use client";

import Link from "next/link";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  Brain,
  Briefcase,
  Megaphone,
  Headphones,
  Microscope,
  BarChart3,
  Users,
  type LucideIcon,
} from "lucide-react";
import { ThemeToggle } from "./theme-toggle";

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
  Icon: LucideIcon;
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
    Icon: Brain,
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
    Icon: Briefcase,
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
    Icon: Megaphone,
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
    Icon: Headphones,
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
    Icon: Microscope,
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
    Icon: BarChart3,
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
    Icon: Users,
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

const STATUS_DOT: Record<Status, string> = {
  idle: "",
  running: "is-active",
  done: "is-online",
};

// Orbital geometry. CEO sits at the center (null); satellites are placed
// around it at fixed angles (degrees, 0 = east, clockwise positive).
const ORBIT: Record<AgentKey, number | null> = {
  ceo: null,
  sales: -90,
  marketing: -30,
  support: 30,
  research: 90,
  finance: 150,
  hiring: 210,
};

const SAT_RADIUS = 36; // % of the orbital square / viewBox units

function nodePos(angleDeg: number) {
  const rad = (angleDeg * Math.PI) / 180;
  return {
    x: 50 + SAT_RADIUS * Math.cos(rad),
    y: 50 + SAT_RADIUS * Math.sin(rad),
  };
}

const WS_URL = "wss://65-0-30-8.sslip.io/ws";

// Reveals an element once it scrolls into view (one-shot).
function useInView<T extends HTMLElement>() {
  const ref = useRef<T | null>(null);
  const [inView, setInView] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (typeof IntersectionObserver === "undefined") {
      const id = requestAnimationFrame(() => setInView(true));
      return () => cancelAnimationFrame(id);
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting) {
          setInView(true);
          observer.disconnect();
        }
      },
      { threshold: 0.18, rootMargin: "0px 0px -10% 0px" }
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  return { ref, inView };
}

const svgProps = {
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.8,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  width: 20,
  height: 20,
};

function IconAssign() {
  return (
    <svg {...svgProps} aria-hidden="true">
      <rect x="8" y="2" width="8" height="4" rx="1" />
      <path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2" />
      <path d="m9 14 2 2 4-4" />
    </svg>
  );
}

function IconCollaborate() {
  return (
    <svg {...svgProps} aria-hidden="true">
      <circle cx="18" cy="5" r="3" />
      <circle cx="6" cy="12" r="3" />
      <circle cx="18" cy="19" r="3" />
      <line x1="8.59" y1="13.51" x2="15.42" y2="17.49" />
      <line x1="15.41" y1="6.51" x2="8.59" y2="10.49" />
    </svg>
  );
}

function IconRealtime() {
  return (
    <svg {...svgProps} aria-hidden="true">
      <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
    </svg>
  );
}

function IconUnified() {
  return (
    <svg {...svgProps} aria-hidden="true">
      <rect x="3" y="3" width="7" height="9" rx="1" />
      <rect x="14" y="3" width="7" height="5" rx="1" />
      <rect x="14" y="12" width="7" height="9" rx="1" />
      <rect x="3" y="16" width="7" height="5" rx="1" />
    </svg>
  );
}

const STEPS: { title: string; desc: string; icon: ReactNode }[] = [
  {
    title: "Assign a task",
    desc: "You give a task to any agent — or hand it to the CEO agent for automatic delegation.",
    icon: <IconAssign />,
  },
  {
    title: "Agents collaborate",
    desc: "The CEO analyzes the task and delegates the right pieces to Sales, Marketing, Support, Research, Finance and Hiring.",
    icon: <IconCollaborate />,
  },
  {
    title: "Real-time execution",
    desc: "Each agent processes its part with the LLM and streams results back live over WebSocket.",
    icon: <IconRealtime />,
  },
  {
    title: "Unified results",
    desc: "You get a consolidated response with every agent's contribution — all from one dashboard.",
    icon: <IconUnified />,
  },
];

export default function Home() {
  const [agents, setAgents] =
    useState<Record<AgentKey, AgentState>>(INITIAL_STATE);
  const [connected, setConnected] = useState(false);
  const [selected, setSelected] = useState<AgentKey>("ceo");
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
        ? "border-online/40 text-online bg-online/10"
        : "border-rose-500/40 text-rose-300 bg-rose-500/10",
    [connected]
  );

  // Lines glow when the CEO is orchestrating, when a satellite is processing,
  // or when the CEO's latest run delegated work to that satellite.
  const ceoDelegations = useMemo(
    () => new Set(agents.ceo.delegations.map((d) => d.agent)),
    [agents.ceo.delegations]
  );

  const isLineActive = useCallback(
    (key: AgentKey) =>
      agents.ceo.status === "running" ||
      agents[key].status === "running" ||
      ceoDelegations.has(key),
    [agents, ceoDelegations]
  );

  const handleTilt = useCallback((e: React.MouseEvent<HTMLElement>) => {
    const el = e.currentTarget;
    const rect = el.getBoundingClientRect();
    const px = (e.clientX - rect.left) / rect.width - 0.5;
    const py = (e.clientY - rect.top) / rect.height - 0.5;
    el.style.setProperty("--ry", `${(px * 6).toFixed(2)}deg`);
    el.style.setProperty("--rx", `${(-py * 6).toFixed(2)}deg`);
  }, []);

  const resetTilt = useCallback((e: React.MouseEvent<HTMLElement>) => {
    const el = e.currentTarget;
    el.style.setProperty("--rx", "0deg");
    el.style.setProperty("--ry", "0deg");
  }, []);

  const selectedConfig = AGENTS.find((a) => a.key === selected) ?? AGENTS[0];
  const satellites = AGENTS.filter((a) => a.key !== "ceo");
  const ceoConfig = AGENTS.find((a) => a.key === "ceo")!;
  const selState = agents[selected];

  const { ref: flowLineRef, inView: flowLineIn } =
    useInView<HTMLSpanElement>();
  const { ref: howRef, inView: howIn } = useInView<HTMLElement>();

  return (
    <main className="min-h-screen w-full px-4 py-10 sm:px-8">
      <div className="mx-auto max-w-7xl">
        <header className="mb-10 flex flex-col gap-5 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <span className="mono text-[11px] uppercase tracking-[0.3em] text-accent">
              Multi-Agent OS
            </span>
            <h1 className="mt-2 font-display text-4xl font-bold tracking-tight text-ink sm:text-5xl">
              Agent<span className="text-accent">Forge</span>
            </h1>
            <p className="mt-2 max-w-md text-sm text-muted">
              An autonomous command center orchestrating seven specialist agents
              in real time.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2.5">
            <ThemeToggle />
            <Link
              href="/improve"
              className="inline-flex w-fit items-center gap-2 rounded-xl border border-hairline bg-panel px-4 py-2 text-sm font-medium text-ink/80 backdrop-blur transition-colors hover:border-accent/50 hover:text-ink"
            >
              ⚡ Improve
            </Link>
            <Link
              href="/autonomous"
              className="inline-flex w-fit items-center gap-2 rounded-xl border border-hairline bg-panel px-4 py-2 text-sm font-medium text-ink/80 backdrop-blur transition-colors hover:border-accent/50 hover:text-ink"
            >
              ⚡ Autonomous
            </Link>
            <Link
              href="/twin"
              className="inline-flex w-fit items-center gap-2 rounded-xl border border-hairline bg-panel px-4 py-2 text-sm font-medium text-ink/80 backdrop-blur transition-colors hover:border-accent/50 hover:text-ink"
            >
              📊 Digital Twin
            </Link>
            <span
              className={`inline-flex w-fit items-center gap-2 rounded-xl border px-3 py-2 text-xs font-medium ${connectionBadge}`}
            >
              <span className={`status-dot ${connected ? "is-online" : ""}`} />
              <span className="mono uppercase tracking-wider">
                {connected ? "Online" : "Offline"}
              </span>
            </span>
          </div>
        </header>

        <div className="grid grid-cols-1 gap-8 lg:grid-cols-[minmax(0,1fr)_minmax(0,440px)]">
          {/* Orbital network — tablet & desktop */}
          <section className="hidden sm:flex sm:flex-col">
            <div className="glass relative mx-auto aspect-square w-full max-w-[540px] p-4">
              <div
                className="grid-bg pointer-events-none absolute inset-0 rounded-2xl"
                aria-hidden="true"
              />
              <svg
                viewBox="0 0 100 100"
                className="absolute inset-0 h-full w-full"
                aria-hidden="true"
              >
                <defs>
                  <linearGradient
                    id="lineGlow"
                    x1="0"
                    y1="0"
                    x2="1"
                    y2="1"
                  >
                    <stop offset="0%" stopColor="#6366F1" />
                    <stop offset="100%" stopColor="#22D3EE" />
                  </linearGradient>
                </defs>
                {satellites.map((config) => {
                  const angle = ORBIT[config.key] ?? 0;
                  const p = nodePos(angle);
                  return (
                    <line
                      key={config.key}
                      x1="50"
                      y1="50"
                      x2={p.x}
                      y2={p.y}
                      className={`orbit-line ${
                        isLineActive(config.key) ? "is-active" : ""
                      }`}
                    />
                  );
                })}
              </svg>

              {/* CEO core node — centered at the exact middle of the orbit */}
              <div className="absolute left-1/2 top-1/2 z-10 -translate-x-1/2 -translate-y-1/2">
                <button
                  type="button"
                  onClick={() => setSelected("ceo")}
                  style={{ animationDelay: "0ms" }}
                  className={`node-in node-glow node-core flex h-20 w-20 flex-col items-center justify-center rounded-full border bg-surface/80 backdrop-blur transition-transform hover:scale-105 ${
                    agents.ceo.status === "running"
                      ? "is-active border-online/50"
                      : "border-accent/40"
                  } ${selected === "ceo" ? "ring-2 ring-accent" : ""}`}
                >
                  <ceoConfig.Icon
                    className="h-7 w-7 text-accent"
                    strokeWidth={1.7}
                    aria-hidden="true"
                  />
                  <span className="mono mt-1 text-[9px] uppercase tracking-wider text-accent">
                    CEO
                  </span>
                </button>
              </div>

              {/* Satellite nodes */}
              {satellites.map((config, i) => {
                const angle = ORBIT[config.key] ?? 0;
                const p = nodePos(angle);
                const st = agents[config.key].status;
                const SatelliteIcon = config.Icon;
                return (
                  <div
                    key={config.key}
                    className="absolute z-10 -translate-x-1/2 -translate-y-1/2"
                    style={{ left: `${p.x}%`, top: `${p.y}%` }}
                  >
                    <button
                      type="button"
                      onClick={() => setSelected(config.key)}
                      style={{ animationDelay: `${140 + i * 90}ms` }}
                      className={`node-in node-glow relative flex h-14 w-14 items-center justify-center rounded-full border bg-surface/70 backdrop-blur transition-[transform,border-color] hover:scale-110 ${
                        st === "running"
                          ? "is-active border-online/50"
                          : "border-hairline"
                      } ${
                        selected === config.key
                          ? "ring-2 ring-accent"
                          : "hover:border-accent/50"
                      }`}
                    >
                      <SatelliteIcon
                        className="h-6 w-6 text-ink/80"
                        strokeWidth={1.7}
                        aria-hidden="true"
                      />
                      <span
                        className={`status-dot ${STATUS_DOT[st]} absolute -right-0.5 -top-0.5`}
                      />
                      <span className="mono pointer-events-none absolute top-[112%] left-1/2 -translate-x-1/2 whitespace-nowrap text-[9px] uppercase tracking-wider text-muted">
                        {config.name}
                      </span>
                    </button>
                  </div>
                );
              })}
            </div>
            <p className="mono mt-4 text-center text-[11px] uppercase tracking-[0.2em] text-muted">
              Select a node to command an agent
            </p>
          </section>

          {/* Mobile node selector */}
          <section className="grid grid-cols-3 gap-2.5 sm:hidden">
            {AGENTS.map((config) => {
              const st = agents[config.key].status;
              return (
                <button
                  key={config.key}
                  type="button"
                  onClick={() => setSelected(config.key)}
                  className={`glass flex flex-col items-center gap-1 p-3 text-center transition-shadow ${
                    selected === config.key ? "ring-1 ring-accent" : ""
                  }`}
                >
                  <config.Icon
                    className="h-5 w-5 text-ink/80"
                    strokeWidth={1.7}
                    aria-hidden="true"
                  />
                  <span className="mono text-[9px] uppercase tracking-wider text-muted">
                    {config.name}
                  </span>
                  <span className={`status-dot ${STATUS_DOT[st]}`} />
                </button>
              );
            })}
          </section>

          {/* Command panel */}
          <section className="lg:sticky lg:top-10 lg:self-start">
            <article
              onMouseMove={handleTilt}
              onMouseLeave={resetTilt}
              className="glass glass-hover tilt p-6"
            >
              <div className="mb-5 flex items-start justify-between gap-3">
                <div className="flex items-center gap-3">
                  <span className="flex h-11 w-11 items-center justify-center rounded-xl border border-hairline bg-panel">
                    <selectedConfig.Icon
                      className="h-5 w-5 text-ink/90"
                      strokeWidth={1.7}
                      aria-hidden="true"
                    />
                  </span>
                  <div>
                    <h2 className="font-display text-xl font-semibold text-ink">
                      {selectedConfig.name} Agent
                    </h2>
                    <span className="mono text-[11px] uppercase tracking-wider text-muted">
                      ID · agent_{selected}
                    </span>
                  </div>
                </div>
                <span className="inline-flex items-center gap-2 rounded-full border border-hairline bg-panel px-2.5 py-1">
                  <span className={`status-dot ${STATUS_DOT[selState.status]}`} />
                  <span className="mono text-[10px] uppercase tracking-wider text-muted">
                    {selState.status === "running"
                      ? "processing"
                      : selState.status === "done"
                        ? "online"
                        : "idle"}
                  </span>
                </span>
              </div>

              <textarea
                value={selState.task}
                onChange={(e) =>
                  updateAgent(selected, { task: e.target.value })
                }
                placeholder={selectedConfig.placeholder}
                rows={3}
                className="w-full resize-none rounded-xl border border-hairline bg-field p-3 text-sm text-ink placeholder-muted/70 outline-none transition focus:border-accent/50 focus:ring-2 focus:ring-accent/30"
              />

              <button
                onClick={() => runAgent(selected)}
                disabled={selState.status === "running" || !selState.task.trim()}
                className="mt-3 inline-flex w-full items-center justify-center gap-2 rounded-xl bg-accent px-4 py-2.5 text-sm font-semibold text-white shadow-[0_10px_30px_-10px_rgba(99,102,241,0.8)] transition hover:bg-accent-soft disabled:cursor-not-allowed disabled:opacity-40"
              >
                {selState.status === "running" ? (
                  <>
                    <span className="h-4 w-4 animate-spin rounded-full border-2 border-white/30 border-t-white" />
                    Running…
                  </>
                ) : (
                  "Run Agent"
                )}
              </button>

              <div className="mt-5">
                <p className="mono mb-2 text-[10px] uppercase tracking-[0.2em] text-muted">
                  Response
                </p>
                <div className="min-h-[140px] rounded-xl border border-hairline bg-field p-4 text-sm leading-relaxed">
                  {selState.status === "running" && (
                    <p className="animate-pulse text-muted">Thinking…</p>
                  )}
                  {selState.error && (
                    <p className="text-rose-300">{selState.error}</p>
                  )}
                  {!selState.error && selState.result && (
                    <p className="whitespace-pre-wrap text-ink/90">
                      {selState.delegations.length > 0
                        ? selState.primaryResponse || selState.result
                        : selState.result}
                    </p>
                  )}
                  {selState.status === "idle" &&
                    !selState.result &&
                    !selState.error && (
                      <p className="text-muted/60">
                        Run the agent to see its response here.
                      </p>
                    )}
                </div>

                {selState.delegations.length > 0 && (
                  <div className="mt-5">
                    <p className="mono mb-3 text-[10px] uppercase tracking-[0.2em] text-muted">
                      Delegated Tasks
                    </p>
                    <div className="flex flex-col gap-3">
                      {selState.delegations.map((delegation, index) => {
                        const sub = AGENTS.find(
                          (a) => a.key === delegation.agent
                        );
                        return (
                          <div
                            key={`${delegation.agent}-${index}`}
                            className="rounded-xl border border-hairline bg-panel p-3"
                          >
                            <div className="mb-1 flex items-center gap-2">
                              {sub && (
                                <sub.Icon
                                  className="h-4 w-4 text-accent-soft"
                                  strokeWidth={1.7}
                                  aria-hidden="true"
                                />
                              )}
                              <span className="text-sm font-semibold capitalize text-accent-soft">
                                {delegation.agent} Agent
                              </span>
                            </div>
                            <p className="mb-2 text-xs italic text-muted">
                              {delegation.task}
                            </p>
                            <p className="whitespace-pre-wrap text-sm text-ink/90">
                              {delegation.response}
                            </p>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                )}
              </div>
            </article>
          </section>
        </div>

        {/* Connecting line that draws itself in on scroll */}
        <div className="mt-12 flex justify-center">
          <span
            ref={flowLineRef}
            className={`draw-line ${flowLineIn ? "in" : ""}`}
            aria-hidden="true"
          />
        </div>

        {/* How it works */}
        <section
          ref={howRef}
          className={`reveal mt-2 ${howIn ? "in" : ""}`}
        >
          <div className="mb-8 text-center">
            <span className="mono text-[11px] uppercase tracking-[0.3em] text-accent">
              Workflow
            </span>
            <h2 className="mt-2 font-display text-2xl font-bold tracking-tight text-ink sm:text-3xl">
              How It Works
            </h2>
            <p className="mx-auto mt-2 max-w-md text-sm text-muted">
              From a single prompt to a coordinated multi-agent response.
            </p>
          </div>

          <ol className="relative grid grid-cols-1 gap-5 md:grid-cols-4">
            {/* Horizontal flow line on desktop */}
            <div
              className="pointer-events-none absolute left-0 right-0 top-9 hidden h-px bg-gradient-to-r from-transparent via-accent/40 to-transparent md:block"
              aria-hidden="true"
            />
            {STEPS.map((step, i) => (
              <li
                key={step.title}
                className="reveal-step glass relative flex flex-col p-5"
                style={{ transitionDelay: `${i * 110}ms` }}
              >
                <div className="flex items-center justify-between">
                  <span className="flex h-10 w-10 items-center justify-center rounded-xl border border-accent/30 bg-accent/10 text-accent">
                    {step.icon}
                  </span>
                  <span className="mono text-3xl font-bold leading-none text-ink/[0.06]">
                    0{i + 1}
                  </span>
                </div>
                <h3 className="mt-4 font-display text-base font-semibold text-ink">
                  {step.title}
                </h3>
                <p className="mt-1.5 text-sm leading-relaxed text-muted">
                  {step.desc}
                </p>
                {/* Flow arrow between steps (desktop) */}
                {i < STEPS.length - 1 && (
                  <span
                    className="mono pointer-events-none absolute -right-3.5 top-9 hidden -translate-y-1/2 text-accent/60 md:block"
                    aria-hidden="true"
                  >
                    →
                  </span>
                )}
              </li>
            ))}
          </ol>
        </section>
      </div>
    </main>
  );
}

"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { useSession } from "next-auth/react";
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
import { ThemeToggle } from "../theme-toggle";

const API_BASE = "https://65-0-30-8.sslip.io";

// Same agent identity (icon + accent) used on the main dashboard in page.tsx.
const AGENT_META: Record<
  string,
  { label: string; Icon: LucideIcon; accent: string }
> = {
  ceo: { label: "CEO", Icon: Brain, accent: "text-amber-400" },
  sales: { label: "Sales", Icon: Briefcase, accent: "text-blue-400" },
  marketing: { label: "Marketing", Icon: Megaphone, accent: "text-purple-400" },
  support: { label: "Support", Icon: Headphones, accent: "text-green-400" },
  research: { label: "Research", Icon: Microscope, accent: "text-orange-400" },
  finance: { label: "Finance", Icon: BarChart3, accent: "text-emerald-400" },
  hiring: { label: "Hiring", Icon: Users, accent: "text-pink-400" },
};

const FALLBACK_META: { label: string; Icon: LucideIcon; accent: string } = {
  label: "Agent",
  Icon: Brain,
  accent: "text-accent",
};

// Duplicated from page.tsx: strips common Markdown syntax so raw LLM output
// reads as clean plain text (no literal **bold**, *, `code`, or # markers).
function cleanMarkdown(text: string): string {
  if (!text) return text;
  return text
    .replace(/\*\*(.+?)\*\*/g, "$1") // **bold** -> bold
    .replace(/__(.+?)__/g, "$1") // __bold__ -> bold
    .replace(/`([^`]+)`/g, "$1") // `code` -> code
    .replace(/^\s{0,3}#{1,6}\s+/gm, "") // # headings -> drop marker
    .replace(/^\s*[*+-]\s+/gm, "- ") // "* item" / "+ item" -> "- item"
    .replace(/\*(\S[^*]*?\S|\S)\*/g, "$1") // *italic* -> italic
    .replace(/(^|[^_])_([^_]+)_(?!_)/g, "$1$2") // _italic_ -> italic
    .trim();
}

// Backend stores naive UTC timestamps (no trailing "Z"); treat them as UTC.
function asUtcDate(iso: string): Date {
  return new Date(/[zZ]|[+-]\d{2}:?\d{2}$/.test(iso) ? iso : `${iso}Z`);
}

function formatRelativeTime(iso: string): string {
  const date = asUtcDate(iso);
  const ms = Date.now() - date.getTime();
  if (Number.isNaN(ms)) return iso;
  const sec = Math.round(ms / 1000);
  if (sec < 60) return "just now";
  const min = Math.round(sec / 60);
  if (min < 60) return `${min} minute${min === 1 ? "" : "s"} ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr} hour${hr === 1 ? "" : "s"} ago`;
  const day = Math.round(hr / 24);
  if (day < 30) return `${day} day${day === 1 ? "" : "s"} ago`;
  return date.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

interface HistoryEntry {
  agent_name: string;
  summary: string;
  created_at: string;
}

const COLLAPSE_LIMIT = 280;

export default function HistoryPage() {
  const { data: session, status } = useSession();
  const userEmail = session?.user?.email ?? null;

  const [entries, setEntries] = useState<HistoryEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  const loadHistory = useCallback(async () => {
    if (!userEmail) return;
    setLoading(true);
    setError("");
    try {
      const res = await fetch(
        `${API_BASE}/history/${encodeURIComponent(userEmail)}`
      );
      if (!res.ok) throw new Error(`Request failed (${res.status})`);
      const data = (await res.json()) as HistoryEntry[];
      setEntries(Array.isArray(data) ? data : []);
      setExpanded(new Set());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load history");
    } finally {
      setLoading(false);
    }
  }, [userEmail]);

  useEffect(() => {
    // Intentional one-shot fetch on mount / when the signed-in user changes.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    loadHistory();
  }, [loadHistory]);

  const toggleExpanded = (index: number) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  };

  return (
    <main className="min-h-screen w-full px-4 py-10 sm:px-8">
      <div className="mx-auto max-w-3xl">
        <header className="mb-10 flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <span className="mono text-[11px] uppercase tracking-[0.3em] text-accent">
              Activity Log
            </span>
            <h1 className="mt-2 font-display text-4xl font-bold tracking-tight text-ink sm:text-5xl">
              History
            </h1>
            <p className="mt-2 text-sm text-muted">
              Every response your agents have produced, newest first.
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

        {status === "loading" ? (
          <p className="glass p-6 text-center text-muted">Loading session…</p>
        ) : !userEmail ? (
          <div className="glass p-8 text-center">
            <p className="text-ink/90">Sign in to view your history.</p>
            <p className="mt-1 text-sm text-muted">
              Your agent activity is tied to your account.
            </p>
            <Link
              href="/"
              className="mt-4 inline-flex w-fit items-center gap-2 rounded-xl border border-hairline bg-panel px-4 py-2 text-sm font-medium text-ink/80 backdrop-blur transition-colors hover:border-accent/50 hover:text-ink"
            >
              Go to sign in
            </Link>
          </div>
        ) : loading ? (
          <div className="flex flex-col gap-4">
            {[0, 1, 2].map((i) => (
              <div
                key={i}
                className="glass h-28 animate-pulse rounded-2xl opacity-60"
              />
            ))}
          </div>
        ) : error ? (
          <p className="mb-6 rounded-xl border border-rose-500/30 bg-rose-500/10 p-4 text-sm text-rose-300">
            {error}
          </p>
        ) : entries.length === 0 ? (
          <p className="glass p-8 text-center text-muted">
            No history yet — run an agent to see it here.
          </p>
        ) : (
          <div className="flex flex-col gap-4">
            {entries.map((entry, index) => {
              const meta = AGENT_META[entry.agent_name] ?? FALLBACK_META;
              const { Icon } = meta;
              const cleaned = cleanMarkdown(entry.summary ?? "");
              const isLong = cleaned.length > COLLAPSE_LIMIT;
              const isOpen = expanded.has(index);
              const shown =
                isOpen || !isLong
                  ? cleaned
                  : `${cleaned.slice(0, COLLAPSE_LIMIT).trimEnd()}…`;

              return (
                <article key={index} className="glass glass-hover p-5">
                  <div className="mb-3 flex items-center justify-between gap-3">
                    <div className="flex items-center gap-2.5">
                      <span className="flex h-9 w-9 items-center justify-center rounded-xl border border-hairline bg-panel">
                        <Icon
                          className={`h-4 w-4 ${meta.accent}`}
                          strokeWidth={1.7}
                          aria-hidden="true"
                        />
                      </span>
                      <span
                        className={`text-sm font-semibold ${meta.accent}`}
                      >
                        {meta.label}
                      </span>
                    </div>
                    <time
                      dateTime={entry.created_at}
                      title={asUtcDate(entry.created_at).toLocaleString()}
                      className="mono text-[11px] uppercase tracking-wider text-muted"
                    >
                      {formatRelativeTime(entry.created_at)}
                    </time>
                  </div>

                  <p className="whitespace-pre-wrap text-sm leading-relaxed text-ink/90">
                    {shown}
                  </p>

                  {isLong && (
                    <button
                      type="button"
                      onClick={() => toggleExpanded(index)}
                      className="mt-3 text-xs font-medium text-accent transition-colors hover:text-accent-soft"
                    >
                      {isOpen ? "Show less" : "Show more"}
                    </button>
                  )}
                </article>
              );
            })}
          </div>
        )}
      </div>
    </main>
  );
}

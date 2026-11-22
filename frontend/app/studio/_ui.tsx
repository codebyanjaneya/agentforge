"use client";

// Shared Studio primitives (M3-C): small, accessible building blocks. Colour always carries a text label too, so state is
// never conveyed by colour alone.

import Link from "next/link";
import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { signIn } from "next-auth/react";
import { explainError, type Explained, type Tone } from "@/lib/studio/view";
import type { PlanTaskAgent } from "@/lib/studio/types";

export function Badge({ tone = "idle", children, pulse = false }: { tone?: Tone; children: ReactNode; pulse?: boolean }) {
  return (
    <span className="st-badge" data-tone={tone}>
      {pulse ? <span className="st-pulse" aria-hidden="true" /> : null}
      {children}
    </span>
  );
}

export function PageHead({ eyebrow, title, lede, actions }: { eyebrow?: string; title: ReactNode; lede?: ReactNode; actions?: ReactNode }) {
  return (
    <header className="st-rise flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0">
        {eyebrow ? <div className="st-eyebrow">{eyebrow}</div> : null}
        <h1 className="st-h1">{title}</h1>
        {lede ? <p className="st-lede">{lede}</p> : null}
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </header>
  );
}

export function Section({ title, aside, children, id }: { title: ReactNode; aside?: ReactNode; children: ReactNode; id?: string }) {
  const heading = useId();
  return (
    <section className="st-section" aria-labelledby={heading} id={id}>
      <div className="st-section-head">
        <h2 className="st-h2" id={heading}>
          {title}
        </h2>
        {aside ? <div className="st-muted text-sm">{aside}</div> : null}
      </div>
      {children}
    </section>
  );
}

export function Notice({ tone = "info", title, children, role }: { tone?: "bad" | "warn" | "good" | "info"; title?: ReactNode; children?: ReactNode; role?: "alert" | "status" }) {
  return (
    <div className="st-notice st-rise" data-tone={tone} role={role ?? (tone === "bad" ? "alert" : "status")}>
      {title ? <div className="st-notice-title">{title}</div> : null}
      {children}
    </div>
  );
}

/** A backend/client error, explained in plain language with the backend's own findings listed. */
export function ErrorNotice({ error, onRetry, fallbackTitle }: { error: Explained | unknown; onRetry?: () => void; fallbackTitle?: string }) {
  const e = isExplained(error) ? error : explainError(error, fallbackTitle);
  return (
    <Notice tone="bad" title={e.title}>
      <div>{e.message}</div>
      {e.items.length ? (
        <ul>
          {e.items.map((item, i) => (
            <li key={i}>{item}</li>
          ))}
        </ul>
      ) : null}
      <div className="mt-2 flex flex-wrap gap-2">
        {e.needsSignIn ? (
          <button className="st-btn st-btn-sm st-btn-primary" onClick={() => void signIn("google")}>
            Sign in again
          </button>
        ) : null}
        {onRetry ? (
          <button className="st-btn st-btn-sm" onClick={onRetry}>
            {e.stale ? "Reload" : "Try again"}
          </button>
        ) : null}
      </div>
    </Notice>
  );
}

const isExplained = (value: unknown): value is Explained =>
  typeof value === "object" && value !== null && "title" in value && "items" in value && "fields" in value;

export function EmptyState({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="st-rise py-10 text-center">
      <div className="st-h2">{title}</div>
      {children ? <p className="st-lede mx-auto">{children}</p> : null}
      {action ? <div className="mt-4 flex justify-center gap-2">{action}</div> : null}
    </div>
  );
}

export function Skeleton({ lines = 3, height = "3.2rem" }: { lines?: number; height?: string }) {
  return (
    <div className="grid gap-3 py-3" aria-busy="true" aria-label="Loading">
      {Array.from({ length: lines }, (_, i) => (
        <div key={i} className="st-skel" style={{ height }} />
      ))}
    </div>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="inline-flex items-center gap-2" role="status">
      <span className="st-spin" aria-hidden="true" />
      {label ? <span className="text-sm">{label}</span> : <span className="st-visually-hidden">Loading</span>}
    </span>
  );
}

export function Field({ label, hint, error, children, htmlFor }: { label: string; hint?: ReactNode; error?: string; children: ReactNode; htmlFor: string }) {
  return (
    <div className="st-field">
      <label className="st-label" htmlFor={htmlFor}>
        {label}
      </label>
      {children}
      {error ? (
        <div className="st-error-text" id={`${htmlFor}-error`}>
          {error}
        </div>
      ) : hint ? (
        <div className="st-hint">{hint}</div>
      ) : null}
    </div>
  );
}

export function AgentChip({ agent }: { agent: Pick<PlanTaskAgent, "source" | "name" | "version"> }) {
  return (
    <span className="st-chip" data-kind={agent.source} title={agent.source === "custom" ? "Your custom agent, pinned to an exact version" : "Built-in AgentForge agent"}>
      {agent.name}
      {agent.source === "custom" && agent.version ? <span className="st-mono">v{agent.version}</span> : <span className="st-muted">built-in</span>}
    </span>
  );
}

/** A modal confirmation built on the native <dialog>: focus is trapped, Escape cancels, and focus starts on the safe choice. */
export function ConfirmDialog({
  open,
  title,
  children,
  confirmLabel,
  tone = "primary",
  busy = false,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  children: ReactNode;
  confirmLabel: string;
  tone?: "primary" | "danger";
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId(); // one dialog per id: several dialogs live on one page, and a shared id would give every one the first dialog's title as its accessible name
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);
  return (
    <dialog ref={ref} className="st-dialog" aria-labelledby={titleId} onCancel={(event) => { event.preventDefault(); if (!busy) onCancel(); }}>
      <h2 className="st-h2" id={titleId}>
        {title}
      </h2>
      <div className="mt-3 text-sm leading-relaxed" style={{ color: "var(--st-ink-2)" }}>
        {children}
      </div>
      <div className="mt-5 flex justify-end gap-2">
        <button className="st-btn" onClick={onCancel} disabled={busy} autoFocus>
          Not now
        </button>
        <button className={`st-btn ${tone === "danger" ? "st-btn-danger" : "st-btn-primary"}`} onClick={onConfirm} disabled={busy}>
          {busy ? <Spinner /> : null}
          {confirmLabel}
        </button>
      </div>
    </dialog>
  );
}

export function BackLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <Link href={href} className="st-muted inline-flex items-center gap-1 text-sm no-underline hover:underline">
      ← {children}
    </Link>
  );
}

// ------------------------------------------------------------------------------------------------ data loading hook

/**
 * Loads data once per dependency change, ignores stale answers and answers after unmount, and exposes a reload.
 * Independent loaders never block each other: each section of a page owns its own loader.
 */
export function useLoader<T>(load: () => Promise<T>, deps: readonly unknown[], enabled = true) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Explained | null>(null);
  const [loading, setLoading] = useState(enabled);
  const counter = useRef(0);
  const loadRef = useRef(load);
  useEffect(() => {
    loadRef.current = load; // always the latest closure; the dependency list below decides WHEN to reload
  });
  const reload = useCallback(() => {
    const mine = ++counter.current;
    setLoading(true);
    setError(null);
    loadRef.current().then(
      (value) => {
        if (mine !== counter.current) return;
        setData(value);
        setLoading(false);
      },
      (e: unknown) => {
        if (mine !== counter.current) return;
        setError(explainError(e));
        setLoading(false);
      },
    );
  }, []);
  useEffect(() => {
    if (!enabled) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    reload();
    return () => {
      counter.current += 1;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, reload, ...deps]);
  return { data, setData, error, loading, reload };
}

/** After a failed submit, move focus to the first invalid field: the error is otherwise off-screen and silent for keyboard and screen-reader users (WCAG 3.3.1). */
export function useFocusFirstError(errors: Record<string, string>) {
  useEffect(() => {
    if (Object.keys(errors).length) document.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus();
  }, [errors]);
}

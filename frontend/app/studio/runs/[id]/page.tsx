"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { getApi } from "@/lib/backend";
import { createPoller, type Poller } from "@/lib/studio/poller";
import type { RunDetail } from "@/lib/studio/types";
import { RUN_STATUS, attention, duration, explainError, explainTask, isTerminalRun, runActions, summarizeRun, type Explained } from "@/lib/studio/view";
import { RunStages } from "../../_run";
import { useMyEmail } from "../../_me";
import { TaskGraph } from "../../_graph";
import { byTask } from "@/lib/studio/tools";
import { approvalsByTask } from "@/lib/studio/approvals";
import { RunHistory } from "../../_approvals";
import { UsagePanel } from "../../_usage";
import { ProviderStatus, RequiredActions } from "../../_provider";
import { SideEffects } from "../../_dispatch";
import { BackLink, Badge, ConfirmDialog, ErrorNotice, Notice, PageHead, Section, Skeleton, Spinner, useLoader } from "../../_ui";

const INTERVAL_MS = 2000;

/** One poller per mounted run page: it stops at a terminal state, pauses while the tab is hidden and is torn down on leave. */
function useRun(runId: string) {
  const [run, setRun] = useState<RunDetail | null>(null);
  const [error, setError] = useState<(Explained & { retrying: boolean }) | null>(null);
  const [loading, setLoading] = useState(true);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const poller = useRef<Poller | null>(null);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    setRun(null);
    setError(null);
    const instance = createPoller<RunDetail>({
      fetch: () => getApi().getRun(runId),
      onData: (value) => {
        setRun(value);
        setError(null);
        setLoading(false);
        setUpdatedAt(Date.now());
      },
      onError: (e, info) => {
        setError({ ...explainError(e, "Can't refresh this run"), retrying: info.willRetry });
        setLoading(false);
      },
      isTerminal: isTerminalRun,
      intervalMs: INTERVAL_MS,
    });
    poller.current = instance;
    instance.start();
    const onVisible = () => {
      if (document.visibilityState === "visible") instance.refresh();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      instance.stop();
      poller.current = null;
    };
  }, [runId]);

  const refresh = useCallback(() => poller.current?.refresh(), []);
  return { run, error, loading, updatedAt, refresh, setRun };
}

export default function RunPage() {
  const { id } = useParams<{ id: string }>();
  const { run, error, loading, updatedAt, refresh, setRun } = useRun(id);
  const plan = useLoader(() => getApi().getPlan(run!.plan_id), [run?.plan_id], !!run);
  // M4-B: the tool calls of this run, shown under their tasks. Re-read after every poll while the run is live and once more when it ends (driven by the run poller: no extra timer).
  const [resolved, setResolved] = useState(0);
  const toolCalls = useLoader(() => getApi().getRunToolInvocations(id), [id, run?.status, run?.status === "running" ? updatedAt : 0, resolved], !!run);
  // M4-C: the human approvals of this run's tool calls (re-read with each progress poll while the run is live: no extra timer)
  const approvals = useLoader(() => getApi().listApprovals({ runId: id, limit: 50 }), [id, run?.status, run?.status === "running" ? updatedAt : 0], !!run);
  const waiting = (approvals.data?.items ?? []).filter((a) => a.status === "pending");
  // the run history (the append-only event log), re-read with each progress poll while the run is live
  const events = useLoader(() => getApi().getRunEvents(id, { limit: 100 }), [id, run?.status, run?.status === "running" ? updatedAt : 0], !!run);
  // M6: what the model requests of this run really used and cost (persisted rows), re-read while the run is live and once more when it ends
  const usage = useLoader(() => getApi().getRunUsage(id), [id, run?.status, run?.status === "running" ? updatedAt : 0], !!run);
  // M6b: what happened while this run's provider requests were retried (re-read with each progress poll while the run is live: no extra timer)
  const providerEvents = useLoader(() => getApi().getRunProviderEvents(id), [id, run?.status, run?.status === "running" ? updatedAt : 0], !!run);
  // P0: unknown side-effect outcomes of this workspace (and whether this person may reconcile them); re-read after a reconciliation
  const reconciliations = useLoader(() => getApi().listReconciliations(), [id, run?.status, resolved], !!run);
  const [now, setNow] = useState(() => Date.now());
  const [confirm, setConfirm] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [cancelError, setCancelError] = useState<Explained | null>(null);
  const router = useRouter();
  const [retryOpen, setRetryOpen] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [acknowledged, setAcknowledged] = useState<string[]>([]);

  /** M4-A2: an explicit retry attempt. Succeeded tasks are reused; uncertain ones run again only if the user ticked them. */
  async function retry() {
    setRetrying(true);
    setCancelError(null);
    try {
      const next = await getApi().retryRun(id, { acknowledge_uncertain: acknowledged });
      setRetryOpen(false);
      router.push(`/studio/runs/${next.id}`);
    } catch (e) {
      setCancelError(explainError(e, "Couldn't start the retry"));
      setRetryOpen(false);
    } finally {
      setRetrying(false);
    }
  }

  const running = run?.status === "running";
  const me = useMyEmail();
  const runPerm = run ? runActions(run, me) : null;
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [running]);

  async function cancel() {
    setCancelling(true);
    setCancelError(null);
    try {
      setRun(await getApi().cancelRun(id));
      refresh();
    } catch (e) {
      setCancelError(explainError(e, "Couldn't cancel the run"));
    } finally {
      setCancelling(false);
      setConfirm(false);
    }
  }

  if (loading && !run) return <div className="mx-auto max-w-4xl"><Skeleton lines={5} height="4rem" /></div>;
  if (!run) {
    return (
      <div className="mx-auto max-w-4xl">
        <BackLink href="/studio/runs">Runs</BackLink>
        <h1 className="sr-only">Run</h1>
        <div className="mt-4">{error ? <ErrorNotice error={error} onRetry={refresh} /> : <Skeleton />}</div>
      </div>
    );
  }

  const summary = summarizeRun(run);
  const meta = RUN_STATUS[run.status];
  const needs = attention(run);
  const elapsed = duration(run.created_at, run.completed_at, now);

  return (
    <div className="mx-auto max-w-4xl">
      <BackLink href={`/studio/plans/${run.plan_id}`}>Plan</BackLink>
      <div className="mt-3">
        <PageHead
          eyebrow={`Run · attempt ${run.attempt} · plan revision ${run.revision}${run.started_by ? ` · started by ${run.started_by}` : ""}`}
          title={plan.data?.objective ?? "Plan run"}
          actions={
            <>
              <Badge tone={meta.tone} pulse={running}>
                {meta.label}
              </Badge>
              {runPerm?.canCancel ? (
                <button className="st-btn st-btn-danger" onClick={() => setConfirm(true)}>
                  Cancel run
                </button>
              ) : null}
            </>
          }
        />
      </div>
      {run.retry_of || run.retried_by || run.tasks.some((t) => t.reused) ? (
        <div className="st-muted mt-2 text-xs">
          {run.retry_of ? (
            <span>
              Retry of <Link href={`/studio/runs/${run.retry_of.id}`}>attempt {run.retry_of.attempt}</Link>.{" "}
            </span>
          ) : null}
          {run.retried_by ? (
            <span>
              Retried in <Link href={`/studio/runs/${run.retried_by.id}`}>attempt {run.retried_by.attempt}</Link>.{" "}
            </span>
          ) : null}
          {run.tasks.some((t) => t.reused) ? <span>Reused without running again: {run.tasks.filter((t) => t.reused).map((t) => `${t.title} (attempt ${t.reused?.attempt})`).join(", ")}.</span> : null}
        </div>
      ) : null}
      <div className="st-muted mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs">
        <span>
          Run ID <span className="st-mono">{run.id}</span>
        </span>
        {elapsed ? (
          <span>
            {running ? "Running for" : "Took"} <span className="st-mono">{elapsed}</span>
          </span>
        ) : null}
      </div>

      <div className="st-panel mt-5 grid gap-3 p-4" role="status" aria-live="polite">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <div className="st-h2">{summary.headline}</div>
            <div className="st-muted text-sm">{summary.detail}</div>
          </div>
          <div className="st-muted text-xs">
            {running ? (
              <span className="inline-flex items-center gap-2">
                <span className="st-pulse" aria-hidden="true" /> Live · checks every {INTERVAL_MS / 1000}s
              </span>
            ) : (
              "Final result"
            )}
            {updatedAt ? <span> · updated {Math.max(0, Math.round((now - updatedAt) / 1000))}s ago</span> : null}
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          {summary.breakdown.map((b) => (
            <Badge key={b.label} tone={b.tone}>
              {b.count} {b.label.toLowerCase()}
            </Badge>
          ))}
        </div>
      </div>

      {error ? (
        <div className="mt-4">
          <ErrorNotice error={{ ...error, message: error.retrying ? `${error.message} Retrying automatically…` : error.message }} onRetry={refresh} />
        </div>
      ) : null}
      {cancelError ? (
        <div className="mt-4">
          <ErrorNotice error={cancelError} />
        </div>
      ) : null}

      {run.status === "partial" || run.status === "failed" || run.status === "cancelled" ? (
        <div className="mt-5">
          <Notice tone={run.status === "partial" ? "warn" : "bad"} title={run.status === "partial" ? "Some work completed — and some did not" : run.status === "cancelled" ? "This run was cancelled" : "This run did not complete"}>
            {needs.length ? (
              <ul>
                {needs.map((t) => (
                  <li key={t.id}>
                    <a href={`#task-${t.id}`}>
                      <strong>{t.title}</strong>
                    </a>{" "}
                    — {t.status}: {explainTask(t, run.tasks) ?? "no further detail"}
                  </li>
                ))}
              </ul>
            ) : null}
            <p className="mt-2 text-sm">
              Nothing is retried automatically. {run.counts.succeeded > 0 ? "Outputs of the tasks that did succeed are kept below. " : ""}To try again, start a new attempt from the{" "}
              <Link href={`/studio/plans/${run.plan_id}`}>plan</Link>.
            </p>
            {run.retry_options?.reason === "reconciliation_required" ? (
              <p className="mt-2 text-sm" data-testid="retry-blocked">
                <strong>Retry is blocked.</strong> A side-effecting action of {run.retry_options.reconcile?.length ? `task${run.retry_options.reconcile.length === 1 ? "" : "s"} ${run.retry_options.reconcile.join(", ")}` : "this run"} has an
                unknown outcome. It may already have happened, so it will not be sent again. Reconcile it under <a href="#side-effects">Side effects</a> first; acknowledging is not enough.
              </p>
            ) : null}
            {runPerm?.canRetry ? (
              <p className="mt-2">
                <button className="st-btn" onClick={() => { setAcknowledged([]); setRetryOpen(true); }}>
                  Retry unfinished tasks
                </button>{" "}
                <span className="st-muted text-xs">Tasks that already succeeded are reused, not run again.</span>
              </p>
            ) : null}
          </Notice>
        </div>
      ) : null}

      <Section title="Tasks" aside="Stage by stage, exactly as the backend reports them.">
        <div className="pt-4">
          {waiting.length ? (
            <Notice tone="warn" title={waiting.length === 1 ? "One action is waiting for your approval" : `${waiting.length} actions are waiting for your approval`}>
              <span className="text-sm">The task is suspended at that step and saved, so it survives a restart. Tasks that do not depend on it keep going. Nothing sensitive runs until you decide, and only that exact call runs afterwards.</span>
            </Notice>
          ) : null}
          <TaskGraph caption="Dependency graph of this run, with each task's current state" tasks={run.tasks.map((t) => ({ id: t.id, title: t.title, depends_on: t.depends_on, level: t.level, state: t.status }))} />
          <RunStages run={run} now={now} tools={byTask(toolCalls.data?.items ?? [])} approvals={approvalsByTask(approvals.data?.items ?? [])} />
        </div>
      </Section>

      {(toolCalls.data?.items ?? []).some((c) => c.dispatch) ? (
        <Section id="side-effects" title="Side effects" aside="Whether each external action was sent, and whether we KNOW what happened. An unknown outcome is never retried automatically.">
          <SideEffects calls={toolCalls.data?.items ?? []} reconciliations={reconciliations.data} onResolved={() => setResolved((n) => n + 1)} />
        </Section>
      ) : null}

      {providerEvents.data && providerEvents.data.items.length ? (
        <Section title="Model provider" aside="Waits, retries and rate limits, as recorded. Nothing is retried that could have taken effect.">
          <ProviderStatus events={providerEvents.data} run={run} />
        </Section>
      ) : null}

      {plan.data && plan.data.revision.tasks.some((t) => t.required_tool) ? (
        <Section title="Required actions" aside="A task is complete only when its required tool was really invoked, not when the agent says it was.">
          <RequiredActions tasks={plan.data.revision.tasks} run={run} calls={toolCalls.data?.items ?? []} pendingTasks={new Set(waiting.map((a) => a.task ?? ""))} />
        </Section>
      ) : null}

      <Section title="Tokens and cost" aside="Real provider usage, recorded per request. A cost appears only when it can be calculated.">
        {usage.data ? <UsagePanel usage={usage.data} /> : <p className="st-muted m-0 py-4 text-sm">{usage.error ? "The usage could not be loaded." : "Loading the usage…"}</p>}
      </Section>

      <Section title="Run history" aside="From the append-only event log: who did what, in order.">
        {events.data ? <RunHistory events={events.data.items} /> : <p className="st-muted m-0 py-4 text-sm">{events.error ? "The history could not be loaded." : "Loading the history…"}</p>}
      </Section>

      {run.outputs.length ? (
        <Section title={run.outputs.length === 1 ? "Final output" : "Final outputs"} aside="From the last tasks of the plan; each is traceable to its execution.">
          <div className="grid gap-4 pt-4">
            {run.outputs.map((o) => (
              <article key={o.task}>
                <div className="flex flex-wrap items-center gap-2">
                  <h3 className="st-h2" style={{ fontSize: "1rem" }}>
                    {o.title}
                  </h3>
                  <span className="st-chip">
                    execution <span className="st-mono">{o.execution_id.slice(0, 8)}</span>
                  </span>
                </div>
                <pre className="st-output" tabIndex={0}>{o.result}</pre>
              </article>
            ))}
          </div>
        </Section>
      ) : null}

      <ConfirmDialog open={retryOpen} title="Retry the unfinished tasks?" confirmLabel="Retry" busy={retrying} onCancel={() => setRetryOpen(false)} onConfirm={() => void retry()}>
        <p>Tasks that already succeeded are kept and their results are reused. Only the other tasks run again, with the same approved agents and versions.</p>
        {run.retry_options?.uncertain.length ? (
          <fieldset className="mt-3">
            <legend className="text-sm">These tasks may already have taken effect before they failed. Tick the ones you accept running again:</legend>
            {run.retry_options.uncertain.map((key) => (
              <label key={key} className="mt-1 flex items-center gap-2 text-sm">
                <input type="checkbox" checked={acknowledged.includes(key)} onChange={(e) => setAcknowledged(e.target.checked ? [...acknowledged, key] : acknowledged.filter((k) => k !== key))} />
                {run.tasks.find((t) => t.id === key)?.title ?? key}
              </label>
            ))}
          </fieldset>
        ) : null}
      </ConfirmDialog>
      <ConfirmDialog open={confirm} title="Cancel this run?" confirmLabel="Cancel run" tone="danger" busy={cancelling} onCancel={() => setConfirm(false)} onConfirm={() => void cancel()}>
        <p>Tasks that are running are stopped and tasks that have not started will not run. Results of tasks that already finished are kept. The plan itself is not changed.</p>
      </ConfirmDialog>
      {cancelling ? <Spinner label="Cancelling…" /> : null}
    </div>
  );
}

"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { getApi } from "@/lib/backend";
import { approvePlanFlow, startRunFlow } from "@/lib/studio/flows";
import type { PlanDetail, PlanRevision, ValidationReport } from "@/lib/studio/types";
import { PLAN_STATUS, RUN_STATUS, explainError, planActions, relativeTime, type Explained } from "@/lib/studio/view";
import { PlanFindings, PlanStages } from "../../_plan";
import { PlanEditor } from "../../_plan-editor";
import { useMyEmail } from "../../_me";
import { TaskGraph } from "../../_graph";
import { BackLink, Badge, ConfirmDialog, EmptyState, ErrorNotice, Notice, PageHead, Section, Skeleton, Spinner, useLoader } from "../../_ui";

type Dialog = null | "approve" | "start" | "cancel";

export default function PlanPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const plan = useLoader(() => getApi().getPlan(id), [id]);
  const agents = useLoader(() => getApi().listAgents("active"), [id]);
  const revisions = useLoader(() => getApi().listRevisions(id), [id, plan.data?.latest_revision]);
  const runs = useLoader(() => getApi().listRuns(id), [id, plan.data?.latest_run?.id, plan.data?.latest_run?.status]);

  const [editing, setEditing] = useState(false);
  const [dialog, setDialog] = useState<Dialog>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<Explained | null>(null);
  const [report, setReport] = useState<ValidationReport | null>(null);
  const [validating, setValidating] = useState(false);
  const [viewing, setViewing] = useState<PlanRevision | null>(null);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setViewing(null);
    setReport(null);
  }, [plan.data?.latest_revision]);

  const p = plan.data;
  const me = useMyEmail();
  const actions = p ? planActions(p, me) : null;
  const shown = viewing ?? p?.revision ?? null;
  const readOnly = viewing !== null;

  async function act(fn: () => Promise<PlanDetail | void>, after?: (result: PlanDetail | void) => void) {
    setBusy(true);
    setActionError(null);
    try {
      const result = await fn();
      if (result && "id" in result) plan.setData(result);
      after?.(result);
    } catch (e) {
      setActionError(explainError(e));
    } finally {
      setBusy(false);
      setDialog(null);
    }
  }

  async function validate() {
    setValidating(true);
    setActionError(null);
    try {
      setReport(await getApi().validatePlan(id));
    } catch (e) {
      setActionError(explainError(e));
    } finally {
      setValidating(false);
    }
  }

  async function openRevision(n: number) {
    if (!p) return;
    if (n === p.latest_revision) return setViewing(null);
    try {
      setViewing(await getApi().getRevision(id, n));
    } catch (e) {
      setActionError(explainError(e));
    }
  }

  if (plan.loading && !p) return <div className="mx-auto max-w-4xl"><Skeleton lines={5} height="4rem" /></div>;
  if (plan.error || !p || !actions || !shown) {
    return (
      <div className="mx-auto max-w-4xl">
        <BackLink href="/studio/plans">Plans</BackLink>
        <h1 className="sr-only">Plan</h1>
        <div className="mt-4">{plan.error ? <ErrorNotice error={plan.error} onRetry={plan.reload} /> : <Skeleton />}</div>
      </div>
    );
  }

  const statusMeta = PLAN_STATUS[p.status];
  const customAgents = [...new Map(shown.tasks.filter((t) => t.agent.source === "custom").map((t) => [t.agent.key, t.agent])).values()];

  return (
    <div className="mx-auto max-w-4xl">
      <BackLink href="/studio/plans">Plans</BackLink>
      <div className="mt-3">
        <PageHead
          eyebrow={`Plan · revision ${p.latest_revision}${p.approved_revision ? ` · approved revision ${p.approved_revision}` : ""}${p.created_by ? ` · created by ${p.created_by}` : ""}`}
          title={p.objective}
          actions={<Badge tone={statusMeta.tone}>{statusMeta.label}</Badge>}
        />
      </div>

      {readOnly ? (
        <div className="mt-4">
          <Notice tone="info" title={`You are viewing revision ${shown.revision} (not the latest)`}>
            <button className="st-btn st-btn-sm mt-2" onClick={() => setViewing(null)}>
              Back to revision {p.latest_revision}
            </button>
          </Notice>
        </div>
      ) : null}

      {p.status === "approved" && !p.latest_run ? (
        <div className="mt-4">
          <Notice tone="good" title="Approved — nothing has run yet">
            Revision {p.approved_revision} is approved. Starting the run is a separate step, whenever you are ready.
          </Notice>
        </div>
      ) : null}
      {p.status === "draft" && p.revision.origin === "revised" ? (
        <div className="mt-4">
          <Notice tone="warn" title="Edited — approval needed">
            Your edits created revision {p.latest_revision}. Review and approve it to enable the run.
          </Notice>
        </div>
      ) : null}

      <Section title="How AgentForge understood it">
        <p className="mt-3 leading-relaxed">{shown.interpretation}</p>
        <div className="st-muted mt-1 text-xs">
          {shown.origin === "model" ? `Drafted by ${shown.model ?? "the planner"}` : "Revised by you"} · {relativeTime(shown.created_at)}
        </div>
      </Section>

      <div className="mt-5">
        <PlanFindings warnings={shown.warnings} unsupported={shown.unsupported} />
      </div>

      {report ? (
        <div className="mt-5">
          <Notice tone={report.valid ? "good" : "bad"} title={report.valid ? "Valid against the agents available now" : "This plan would not run as written"}>
            {report.valid ? (
              <div className="mt-1 text-sm">Dependencies are consistent and every assigned agent is available. Order: {report.levels.map((l, i) => `${i + 1}. ${l.join(" + ")}`).join("  →  ")}</div>
            ) : (
              <ul>
                {report.errors.map((e, i) => (
                  <li key={i}>
                    {e.task ? <span className="st-mono">{e.task}: </span> : null}
                    {e.message}
                  </li>
                ))}
              </ul>
            )}
          </Notice>
        </div>
      ) : null}

      <Section title={editing ? "Edit tasks" : `Tasks · ${shown.tasks.length}`} aside={editing ? undefined : "Stages run top to bottom; tasks in the same stage are independent."}>
        <div className="pt-4">
          {editing ? (
            <PlanEditor
              plan={p}
              agents={agents.data?.items ?? []}
              onSaved={(updated) => {
                plan.setData(updated);
                setEditing(false);
                setReport(null);
              }}
              onCancel={() => setEditing(false)}
            />
          ) : (
            <>
              <TaskGraph caption={`Dependency graph of revision ${shown.revision}`} tasks={shown.tasks.map((t) => ({ id: t.id, title: t.title, depends_on: t.depends_on, level: t.level, agent: t.agent.name }))} />
              <PlanStages tasks={shown.tasks} />
            </>
          )}
        </div>
        {customAgents.length && !editing ? (
          <p className="st-hint mt-1">
            Custom agents in this plan are pinned to the exact version shown. Editing an agent later never changes this plan.
          </p>
        ) : null}
      </Section>

      {!editing && !readOnly ? (
        <div className="st-panel sticky bottom-16 z-10 mt-6 grid gap-3 p-4 md:bottom-4" role="region" aria-label="Plan actions">
          {actionError ? <ErrorNotice error={actionError} onRetry={actionError.stale ? plan.reload : undefined} /> : null}
          <div className="flex flex-wrap items-center gap-2">
            {actions.canApprove ? (
              <button className="st-btn st-btn-primary" onClick={() => setDialog("approve")} disabled={busy}>
                Approve this plan
              </button>
            ) : null}
            {p.status === "approved" && actions.canWrite ? (
              <button className="st-btn st-btn-primary" onClick={() => setDialog("start")} disabled={!actions.canStartRun || busy}>
                Start run
              </button>
            ) : null}
            {actions.canRevise ? (
              <button className="st-btn" onClick={() => setEditing(true)} disabled={busy}>
                Edit tasks
              </button>
            ) : null}
            {actions.canValidate ? (
              <button className="st-btn" onClick={() => void validate()} disabled={validating || busy}>
                {validating ? <Spinner /> : null}Validate
              </button>
            ) : null}
            {actions.canCancel ? (
              <button className="st-btn st-btn-danger ml-auto" onClick={() => setDialog("cancel")} disabled={busy}>
                Cancel plan
              </button>
            ) : null}
          </div>
          {actions.startBlockedReason && p.status !== "cancelled" ? <div className="st-hint">{actions.startBlockedReason}</div> : null}
          {p.latest_run ? (
            <div className="text-sm">
              Latest run: <Badge tone={RUN_STATUS[p.latest_run.status].tone}>{RUN_STATUS[p.latest_run.status].label}</Badge>{" "}
              <Link href={`/studio/runs/${p.latest_run.id}`}>open run →</Link>
            </div>
          ) : null}
        </div>
      ) : null}

      <Section title="Revision history" aside={revisions.data ? `${revisions.data.items.length} revision${revisions.data.items.length === 1 ? "" : "s"}` : undefined}>
        {revisions.loading && !revisions.data ? (
          <Skeleton lines={1} height="2.4rem" />
        ) : revisions.error ? (
          <ErrorNotice error={revisions.error} onRetry={revisions.reload} />
        ) : (
          <ul className="m-0 list-none p-0">
            {revisions.data?.items.map((r) => (
              <li key={r.revision} className="st-row" style={{ gridTemplateColumns: "1fr auto" }}>
                <div>
                  <span className="st-mono">r{r.revision}</span> · {r.origin === "model" ? "drafted by the planner" : "revised by you"} · <span className="st-muted">{relativeTime(r.created_at)}</span>
                  {r.revision === p.approved_revision ? <span className="ml-2"><Badge tone="good">approved</Badge></span> : null}
                </div>
                <button className="st-btn st-btn-sm" onClick={() => void openRevision(r.revision)} aria-pressed={shown.revision === r.revision}>
                  {shown.revision === r.revision ? "Viewing" : "View"}
                </button>
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section title="Runs of this plan">
        {runs.loading && !runs.data ? (
          <Skeleton lines={1} height="2.4rem" />
        ) : runs.error ? (
          <ErrorNotice error={runs.error} onRetry={runs.reload} />
        ) : runs.data && runs.data.items.length ? (
          <ul className="m-0 list-none p-0">
            {runs.data.items.map((r) => (
              <li key={r.id}>
                <Link href={`/studio/runs/${r.id}`} className="st-row" style={{ gridTemplateColumns: "1fr auto" }}>
                  <div>
                    <span className="st-row-title">Attempt {r.attempt}</span> <span className="st-muted text-xs">· revision {r.revision} · {relativeTime(r.created_at)} · {r.counts.succeeded}/{r.counts.total} succeeded</span>
                  </div>
                  <Badge tone={RUN_STATUS[r.status].tone} pulse={r.status === "running"}>{RUN_STATUS[r.status].label}</Badge>
                </Link>
              </li>
            ))}
          </ul>
        ) : (
          <EmptyState title="No runs yet">{p.status === "approved" ? "Start the run when you are ready." : "Approve the plan to be able to start a run."}</EmptyState>
        )}
      </Section>

      <ConfirmDialog
        open={dialog === "approve"}
        title={`Approve revision ${p.latest_revision}?`}
        confirmLabel="Approve plan"
        busy={busy}
        onCancel={() => setDialog(null)}
        onConfirm={() => void act(() => approvePlanFlow(getApi(), p))}
      >
        <p>
          You are approving <strong>{p.revision.tasks.length} task{p.revision.tasks.length === 1 ? "" : "s"}</strong> in {p.revision.levels.length} stage{p.revision.levels.length === 1 ? "" : "s"}, assigned to:{" "}
          {[...new Set(p.revision.tasks.map((t) => t.agent.name))].join(", ")}.
        </p>
        <p className="mt-2">
          <strong>Approving does not start any work.</strong> You will start the run yourself as a separate step.
        </p>
      </ConfirmDialog>
      <ConfirmDialog
        open={dialog === "start"}
        title="Start this run now?"
        confirmLabel="Start run"
        busy={busy}
        onCancel={() => setDialog(null)}
        onConfirm={() =>
          void act(
            () => startRunFlow(getApi(), p).then((run) => { router.push(`/studio/runs/${run.id}`); }),
          )
        }
      >
        <p>
          Agents will begin working on revision {p.approved_revision} immediately, in the order shown. Each task uses the agent version approved in the plan.
          You can follow progress live and cancel the run at any time.
        </p>
      </ConfirmDialog>
      <ConfirmDialog
        open={dialog === "cancel"}
        title="Cancel this plan?"
        confirmLabel="Cancel plan"
        tone="danger"
        busy={busy}
        onCancel={() => setDialog(null)}
        onConfirm={() => void act(() => getApi().cancelPlan(id))}
      >
        <p>A cancelled plan cannot be edited, approved or run again. Its history stays readable.</p>
        <p className="mt-2">A run that is already in progress is <strong>not</strong> stopped by this; cancel the run itself to stop it.</p>
      </ConfirmDialog>
    </div>
  );
}

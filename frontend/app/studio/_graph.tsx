"use client";

import { useId } from "react";

/** One node of the dependency graph. `state` is a task-run status when the graph shows a run; omitted for a plan (nothing has happened yet). */
export type GraphTask = { id: string; title: string; depends_on: string[]; level: number; state?: string; agent?: string };

const NODE_W = 188;
const NODE_H = 54;
const GAP_X = 64;
const GAP_Y = 16;
const PAD = 8;
const STATE_TEXT: Record<string, string> = { pending: "Waiting", running: "Running", succeeded: "Succeeded", failed: "Failed", skipped: "Skipped", cancelled: "Cancelled" };

const clip = (text: string, max: number) => (text.length > max ? `${text.slice(0, max - 1)}…` : text);

/**
 * The plan's real dependency graph, drawn from the same `level` and `depends_on` the backend validated (nothing is inferred here). Columns are the stages in which
 * tasks may run; an arrow means "must finish first". State is written as a word on every node (colour only repeats it). A text description of every dependency
 * is attached for assistive technology, and the full task list stays below it on the page.
 */
export function TaskGraph({ tasks, caption }: { tasks: GraphTask[]; caption: string }) {
  const titleId = useId();
  const descId = useId();
  if (tasks.length === 0) return null;
  const levels = [...new Set(tasks.map((t) => t.level))].sort((a, b) => a - b);
  const columns = levels.map((level) => tasks.filter((t) => t.level === level));
  const tallest = Math.max(...columns.map((c) => c.length));
  const height = PAD * 2 + tallest * NODE_H + (tallest - 1) * GAP_Y;
  const width = PAD * 2 + columns.length * NODE_W + (columns.length - 1) * GAP_X;
  const at = new Map<string, { x: number; y: number }>();
  columns.forEach((col, c) => {
    const offset = (height - (col.length * NODE_H + (col.length - 1) * GAP_Y)) / 2; // each stage is centred, so fan-in and fan-out read symmetrically
    col.forEach((task, r) => at.set(task.id, { x: PAD + c * (NODE_W + GAP_X), y: offset + r * (NODE_H + GAP_Y) }));
  });
  const edges = tasks.flatMap((task) => task.depends_on.filter((dep) => at.has(dep) && at.has(task.id)).map((dep) => ({ from: dep, to: task.id })));
  const title = new Map(tasks.map((t) => [t.id, t.title]));
  const description = edges.length ? edges.map((e) => `${title.get(e.from)} must finish before ${title.get(e.to)}`).join(". ") : "No task depends on another: all tasks may run at once.";
  return (
    <figure className="st-graph">
      <div className="st-graph-scroll" role="region" aria-label={`${caption}: scrollable`} tabIndex={0}>
        <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-labelledby={`${titleId} ${descId}`}>
          <title id={titleId}>{caption}</title>
          <desc id={descId}>{description}</desc>
          <defs>
            <marker id={`${titleId}-arrow`} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
              <path d="M0 0 L8 4 L0 8 z" className="st-graph-arrow" />
            </marker>
          </defs>
          {edges.map((e) => {
            const a = at.get(e.from)!;
            const b = at.get(e.to)!;
            const x1 = a.x + NODE_W;
            const y1 = a.y + NODE_H / 2;
            const x2 = b.x;
            const y2 = b.y + NODE_H / 2;
            const mid = (x1 + x2) / 2;
            return <path key={`${e.from}>${e.to}`} d={`M${x1} ${y1} C${mid} ${y1} ${mid} ${y2} ${x2 - 1} ${y2}`} className="st-graph-edge" markerEnd={`url(#${titleId}-arrow)`} />;
          })}
          {tasks.map((task) => {
            const p = at.get(task.id)!;
            const state = task.state;
            return (
              <g key={task.id} transform={`translate(${p.x} ${p.y})`} className="st-graph-node" data-state={state ?? "plan"}>
                <title>{`${task.title}${state ? ` (${STATE_TEXT[state] ?? state})` : ""}`}</title>
                <rect width={NODE_W} height={NODE_H} rx="4" className="st-graph-box" />
                <rect width="4" height={NODE_H} rx="1" className="st-graph-stripe" />
                <text x="14" y="21" className="st-graph-title">
                  {clip(task.title, 27)}
                </text>
                <text x="14" y="40" className="st-graph-sub">
                  {state ? STATE_TEXT[state] ?? state : task.agent ? clip(task.agent, 28) : `Stage ${task.level + 1}`}
                </text>
              </g>
            );
          })}
        </svg>
      </div>
      <figcaption className="st-graph-cap">
        {columns.length === 1 ? "One stage: every task may run at the same time." : `${columns.length} stages, left to right; an arrow means "must finish first".`}
      </figcaption>
    </figure>
  );
}

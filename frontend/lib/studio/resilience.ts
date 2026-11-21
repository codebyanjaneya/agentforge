// Words for provider retry states and required actions (M6b). Pure: every state comes from what the backend recorded; nothing here invents progress.

import type { ProviderTaskState, ToolInvocationItem } from "./types.ts";

type Tone = "good" | "bad" | "warn" | "muted" | "active";

export const PROVIDER_STATE: Record<ProviderTaskState["state"], { label: string; tone: Tone; live: boolean }> = {
  waiting: { label: "Waiting before retry", tone: "warn", live: true },
  retrying: { label: "Retrying", tone: "active", live: true },
  recovered: { label: "Recovered", tone: "good", live: false },
  exhausted: { label: "Rate limit exhausted", tone: "bad", live: false },
  failed: { label: "Provider unavailable", tone: "bad", live: false },
};

const seconds = (ms: number) => `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)} s`;

/** One plain sentence for a task's current provider state, using only recorded facts. `taskStatus` tells a final state from a live one. */
export function providerSentence(state: ProviderTaskState, taskStatus?: string): string {
  switch (state.state) {
    case "waiting":
      return `The model provider asked us to slow down (attempt ${state.attempt}). Waiting${state.wait_ms ? ` ${seconds(state.wait_ms)}` : ""} before trying again. Nothing was lost.`;
    case "retrying":
      return `Trying again now (attempt ${state.attempt}).`;
    case "recovered":
      return `The provider answered on attempt ${state.attempt}. The task carried on from there; nothing was run twice.`;
    case "exhausted": {
      if (state.wait_ms && state.wait_ms > 90000) {
        const minutes = Math.ceil(state.wait_ms / 60000);
        return `The provider asked us to wait about ${minutes} minute${minutes === 1 ? "" : "s"} (a usage limit), longer than a task will wait, so we did not wait. Nothing was lost.${taskStatus === "failed" ? " Retry the run after that time; finished tasks keep their results." : ""}`;
      }
      return `The provider kept rate-limiting this task and the retry budget is spent (${state.attempt} attempt${state.attempt === 1 ? "" : "s"}). ${taskStatus === "failed" ? "The task failed; finished tasks keep their results. Retry the run to try this one again." : ""}`.trim();
    }
    default: {
      const why = state.error_code === "unavailable" ? "was unavailable" : state.error_code === "timeout" ? "did not answer in time" : "refused the request";
      return `The model provider ${why} after ${state.attempt} attempt${state.attempt === 1 ? "" : "s"}. ${taskStatus === "failed" ? "The task failed; retry the run to try it again." : ""}`.trim();
    }
  }
}

export type RequiredActionStatus = { label: string; tone: Tone; detail: string };

/** Where a task's REQUIRED action really stands, from the recorded tool calls and approvals (never from what the agent said). */
export function requiredActionStatus(requiredTool: string, calls: Pick<ToolInvocationItem, "tool" | "status" | "task">[], taskKey: string, pendingApproval: boolean, taskStatus: string): RequiredActionStatus {
  const mine = calls.filter((c) => c.task === taskKey && `${c.tool.id}@${c.tool.version ?? ""}` === requiredTool);
  if (mine.some((c) => c.status === "succeeded")) return { label: "Performed", tone: "good", detail: "The tool was really invoked and finished." };
  if (pendingApproval) return { label: "Waiting for approval", tone: "active", detail: "The exact call is paused for a person's decision. Nothing has been dispatched." };
  if (mine.length) return { label: "Not performed", tone: "bad", detail: "It was requested but refused, rejected or failed, so the task is not complete." };
  if (taskStatus === "failed") return { label: "Not performed", tone: "bad", detail: "The agent never invoked it, so the task was not completed." };
  return { label: "Not yet invoked", tone: "muted", detail: "The task is not complete until this tool is really invoked." };
}

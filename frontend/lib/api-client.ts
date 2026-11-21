// Typed backend API client (M1-H).
//
// Tokens: the 10-minute backend access token is obtained from the same-origin `POST /api/backend-token` endpoint
// (M1-G) and held ONLY in this module's closure (JavaScript memory). It is never written to localStorage,
// sessionStorage, IndexedDB, a cookie, a URL or a log, and is never part of an error. Concurrent callers share one
// in-flight acquisition. A token is renewed `marginSeconds` before it expires.
//
// Requests: every protected call sends `Authorization: Bearer <token>` (no cookies: `credentials: "omit"`). On a 401
// the stale token is dropped, a fresh one is acquired and the request is retried exactly ONCE. That retry is safe
// even for POST/DELETE because the backend rejects an unauthenticated request before it executes anything; no other
// status and no network error is ever retried. Errors are `ApiError`s with a stable `kind` and a fixed
// user-facing message; response bodies are never echoed.

export type ApiErrorKind =
  | "auth"
  | "forbidden"
  | "validation"
  | "conflict"
  | "not_found"
  | "rate_limited"
  | "server"
  | "network"
  | "config";

const MESSAGES: Record<ApiErrorKind, string> = {
  auth: "Your session has expired. Please sign in again.",
  forbidden: "You don't have access to this workspace.",
  validation: "That request wasn't accepted. Please check your input and try again.",
  conflict: "That action conflicts with the current state. Refresh and try again.",
  not_found: "That item no longer exists.",
  rate_limited: "Too many requests. Please wait a moment and try again.",
  server: "The server ran into a problem. Please try again.",
  network: "Can't reach the AgentForge server. Check your connection and try again.",
  config: "AgentForge isn't configured correctly (backend address).",
};

/** The part of an error body that is safe and useful to show: stable codes, field messages and fixed backend texts. */
export type ErrorDetail = {
  code?: string;
  message?: string;
  latestVersion?: number;
  latestRevision?: number;
  limit?: string;
  interpretation?: string;
  errors?: { code: string; task: string | null; message: string }[];
  unsupported?: { requirement: string; reason: string }[];
  fields?: { path: string; message: string }[];
};

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status?: number;
  /** Whitelisted, size-bounded facts from the error body (never the raw body). */
  readonly detail?: ErrorDetail;
  constructor(kind: ApiErrorKind, status?: number, detail?: ErrorDetail) {
    super(MESSAGES[kind]);
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
    this.detail = detail;
  }
  get userMessage(): string {
    return MESSAGES[this.kind];
  }
}

export function errorKindForStatus(status: number): ApiErrorKind {
  if (status === 401) return "auth";
  if (status === 403) return "forbidden";
  if (status === 404) return "not_found";
  if (status === 409) return "conflict";
  if (status === 429) return "rate_limited";
  if (status === 400 || status === 422) return "validation";
  return "server";
}

/** A message safe to show in the UI for any thrown value. */
export function describeError(error: unknown, fallback = "Something went wrong. Please try again."): string {
  return error instanceof ApiError ? error.userMessage : fallback;
}

// ------------------------------------------------------------------------------------------------ token manager

export type BackendToken = { token: string; expiresAt: number };
type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export type TokenManager = {
  get(): Promise<BackendToken>;
  /** Drop the cached token only if it is still the one that was rejected (so a concurrent refresh is not undone). */
  invalidate(staleToken: string): void;
  clear(): void;
};

export function createTokenManager(options: {
  fetchImpl?: FetchLike;
  endpoint?: string;
  now?: () => number; // seconds
  marginSeconds?: number;
}): TokenManager {
  const endpoint = options.endpoint ?? "/api/backend-token";
  const now = options.now ?? (() => Date.now() / 1000);
  const margin = options.marginSeconds ?? 60;
  const doFetch: FetchLike = options.fetchImpl ?? ((input, init) => fetch(input, init));
  let cached: BackendToken | null = null; // memory only
  let inflight: Promise<BackendToken> | null = null;

  async function acquire(): Promise<BackendToken> {
    let response: Response;
    try {
      response = await doFetch(endpoint, { method: "POST", credentials: "same-origin", cache: "no-store" });
    } catch {
      throw new ApiError("network");
    }
    if (!response.ok) throw new ApiError(errorKindForStatus(response.status), response.status);
    let body: unknown;
    try {
      body = await response.json();
    } catch {
      throw new ApiError("server", response.status);
    }
    const { token, expires_at: expiresAt } = (body ?? {}) as { token?: unknown; expires_at?: unknown };
    if (typeof token !== "string" || !token || typeof expiresAt !== "number" || !Number.isFinite(expiresAt) || expiresAt <= now()) {
      throw new ApiError("server", response.status);
    }
    return { token, expiresAt };
  }

  return {
    async get() {
      if (cached && now() < cached.expiresAt - margin) return cached;
      if (!inflight) {
        const attempt = acquire()
          .then((fresh) => {
            cached = fresh;
            return fresh;
          })
          .finally(() => {
            inflight = null;
          });
        inflight = attempt;
      }
      return inflight;
    },
    invalidate(staleToken) {
      if (cached?.token === staleToken) cached = null;
    },
    clear() {
      cached = null;
    },
  };
}

// ------------------------------------------------------------------------------------------------ contracts

import type {
  AgentDefinition, AgentDetail, AgentSummary, PlanDetail, PlanListFilter, PlanPage, PlanRevision, PlanSummary, RevisedTaskInput, RevisionListItem,
  ApprovalItem, ApprovalList, ApprovalStatus, DemoInfo, NotificationCounts, NotificationList, ReconcileInput, ReconciliationList, DispatchView, RunProviderEvents, RunUsage, WorkspaceInfo, JoinedWorkspace, WorkspaceInvitation, WorkspaceInvitationCreated, WorkspaceMember, WorkspaceRole, StarterResult, RunDetail, RunEventPage, RunListItem, RunToolInvocations, TaskRunDetail, ToolCatalog, ToolDetail, ToolInvocationDetail, ToolInvocationPage, TemplateDetail, TemplateSummary, ValidationReport, VersionDetail, VersionInfo,
} from "./studio/types.ts";

export type ActionItem = { action_type: string; description: string; status: string };
export type ExecutionResult = { agent: string; task: string; response: string; actions: ActionItem[]; executed_at: string };
export type ScheduledTask = { id: string; agent: string; task: string; interval_minutes: number; created_at: string };
export type ImprovementReport = {
  agent_name: string;
  strengths: string[];
  weaknesses: string[];
  improvements: string[];
  updated_strategy: string;
  has_history?: boolean;
  analyzed_at?: string;
};
export type HistoryEntry = { id: number; agent_name: string; summary: string; created_at: string };
export type HistoryPage = { items: HistoryEntry[]; next_cursor: string | null };

// ------------------------------------------------------------------------------------------------ error bodies

const text = (value: unknown, max: number): string | undefined => (typeof value === "string" ? value.slice(0, max) : undefined);
const int = (value: unknown): number | undefined => (typeof value === "number" && Number.isInteger(value) ? value : undefined);
function list<T>(value: unknown, pick: (item: Record<string, unknown>) => T | null, max = 25): T[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const out: T[] = [];
  for (const item of value.slice(0, max)) {
    if (item && typeof item === "object") {
      const picked = pick(item as Record<string, unknown>);
      if (picked) out.push(picked);
    }
  }
  return out;
}

/** Reduce an error response body to the whitelisted facts above. Anything else (including a plain-text `detail`) is dropped. */
export function sanitizeErrorBody(body: unknown): ErrorDetail | undefined {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (Array.isArray(detail)) {
    const fields = list(
      detail,
      (e) => {
        const loc = Array.isArray(e.loc) ? e.loc.filter((p) => typeof p === "string" || typeof p === "number") : [];
        const message = text(e.msg, 300);
        return message ? { path: loc.filter((p) => p !== "body").join("."), message } : null;
      },
      40,
    );
    return fields && fields.length ? { fields } : undefined;
  }
  if (!detail || typeof detail !== "object") return undefined;
  const d = detail as Record<string, unknown>;
  const code = typeof d.code === "string" && /^[a-z0-9_]{1,64}$/.test(d.code) ? d.code : undefined;
  const out: ErrorDetail = {
    code,
    message: text(d.message, 400),
    latestVersion: int(d.latest_version),
    latestRevision: int(d.latest_revision),
    limit: text(d.limit, 40),
    interpretation: text(d.interpretation, 800),
  };
  out.errors = list(d.errors, (e) =>
    typeof e.code === "string" && typeof e.message === "string"
      ? { code: text(e.code, 64)!, task: text(e.task, 40) ?? null, message: text(e.message, 400)! }
      : null,
  );
  out.unsupported = list(d.unsupported, (e) =>
    typeof e.requirement === "string" && typeof e.reason === "string" ? { requirement: text(e.requirement, 200)!, reason: text(e.reason, 300)! } : null,
  );
  return Object.fromEntries(Object.entries(out).filter(([, v]) => v !== undefined)) as ErrorDetail;
}

export type ApiClient = {
  // ---- M2-A: templates and custom agents
  listTemplates(): Promise<{ items: TemplateSummary[] }>;
  getTemplate(id: string): Promise<TemplateDetail>;
  instantiateTemplate(id: string, input?: { slug?: string; name?: string }): Promise<AgentDetail>;
  listAgents(status?: "active" | "archived" | "all"): Promise<{ items: AgentSummary[] }>;
  getAgent(id: string): Promise<AgentDetail>;
  createAgent(input: { slug: string; definition: AgentDefinition; change_note?: string | null }): Promise<AgentDetail>;
  updateAgent(id: string, input: { base_version: number; definition: AgentDefinition; change_note?: string | null }): Promise<AgentDetail>;
  archiveAgent(id: string): Promise<AgentDetail>;
  unarchiveAgent(id: string): Promise<AgentDetail>;
  listVersions(id: string): Promise<{ items: VersionInfo[] }>;
  getVersion(id: string, version: number): Promise<VersionDetail>;
  rollbackAgent(id: string, input: { to_version: number; base_version: number }): Promise<AgentDetail>;
  // ---- M3-A: plans (none of these starts execution)
  createPlan(objective: string): Promise<PlanDetail>;
  listPlans(options?: { status?: PlanListFilter; limit?: number; cursor?: string | null }): Promise<PlanPage>;
  getPlan(id: string): Promise<PlanDetail>;
  listRevisions(id: string): Promise<{ items: RevisionListItem[] }>;
  getRevision(id: string, revision: number): Promise<PlanRevision>;
  validatePlan(id: string): Promise<ValidationReport>;
  revisePlan(id: string, input: { base_revision: number; interpretation?: string | null; tasks: RevisedTaskInput[] }): Promise<PlanDetail>;
  approvePlan(id: string, revision: number): Promise<PlanDetail>;
  cancelPlan(id: string): Promise<PlanDetail>;
  // ---- M3-B: plan runs (starting one is its own explicit call)
  startRun(planId: string): Promise<RunDetail>;
  listRuns(planId: string): Promise<{ items: RunListItem[] }>;
  getRun(runId: string): Promise<RunDetail>;
  getTaskRun(runId: string, taskId: string): Promise<TaskRunDetail>;
  cancelRun(runId: string): Promise<RunDetail>;
  // ---- M4-A2: an explicit retry attempt (succeeded tasks are reused; uncertain tasks must be acknowledged) and the run history
  retryRun(runId: string, input?: { acknowledge_uncertain?: string[] }): Promise<RunDetail>;
  getRunEvents(runId: string, options?: { after?: number; limit?: number }): Promise<RunEventPage>;
  // ---- M4-B: the tool registry and the history of real tool calls (read-only; tools only ever run inside an agent's execution)
  listTools(): Promise<ToolCatalog>;
  getTool(toolId: string): Promise<ToolDetail>;
  listToolInvocations(options?: { runId?: string; toolId?: string; agent?: string; status?: string; limit?: number; cursor?: string | null }): Promise<ToolInvocationPage>;
  getToolInvocation(invocationId: string): Promise<ToolInvocationDetail>;
  getRunToolInvocations(runId: string): Promise<RunToolInvocations>;
  // ---- M4-C: human approval of ONE exact tool call. The decision carries only the hash the person was shown (a staleness check: the server recomputes everything)
  listApprovals(options?: { status?: ApprovalStatus; runId?: string; limit?: number }): Promise<ApprovalList>;
  getApproval(approvalId: string): Promise<ApprovalItem>;
  decideApproval(approvalId: string, decision: "approve" | "reject", shownHash?: string): Promise<ApprovalItem>;
  // ---- M5: demo status (a scripted model and a simulated sending tool, when true) and the one-step starter workforce
  getDemo(): Promise<DemoInfo>;
  createStarterWorkforce(): Promise<StarterResult>;
  // ---- M6: workspaces (the server verifies membership of any selected one), notifications (the inbox badge) and per-run usage
  listWorkspaces(): Promise<{ items: WorkspaceInfo[] }>;
  createWorkspace(name: string): Promise<WorkspaceInfo>;
  listWorkspaceMembers(workspaceId: string): Promise<{ items: WorkspaceMember[] }>;
  addWorkspaceMember(workspaceId: string, invitee: string, role: Exclude<WorkspaceRole, "owner">): Promise<WorkspaceMember>;
  // ---- sprint 5: invitations (the secret link is returned ONCE, on creation) and member management
  inviteToWorkspace(workspaceId: string, invitee: string, role: Exclude<WorkspaceRole, "owner">): Promise<WorkspaceInvitationCreated>;
  listInvitations(workspaceId: string): Promise<{ items: WorkspaceInvitation[] }>;
  revokeInvitation(workspaceId: string, invitationId: string): Promise<void>;
  acceptInvitation(token: string): Promise<JoinedWorkspace>;
  changeMemberRole(workspaceId: string, member: string, role: Exclude<WorkspaceRole, "owner">): Promise<WorkspaceMember>;
  removeMember(workspaceId: string, member: string): Promise<void>;
  leaveWorkspace(workspaceId: string): Promise<void>;
  listNotifications(options?: { unread?: boolean; limit?: number }): Promise<NotificationList>;
  getNotificationCounts(): Promise<NotificationCounts>;
  markNotificationRead(notificationId: string): Promise<{ changed: number }>;
  markAllNotificationsRead(): Promise<{ changed: number }>;
  getRunUsage(runId: string): Promise<RunUsage>;
  getRunProviderEvents(runId: string): Promise<RunProviderEvents>;
  // ---- P0: unknown side-effect outcomes (reconciling NEVER dispatches anything: it records evidence and decides what is known)
  listReconciliations(): Promise<ReconciliationList>;
  reconcileInvocation(invocationId: string, input: ReconcileInput): Promise<{ dispatch: DispatchView }>;
  // ---- M1-H
  runAutonomous(agent: string, task: string): Promise<ExecutionResult>;
  listTasks(): Promise<ScheduledTask[]>;
  scheduleTask(input: { agent: string; task: string; interval_minutes: number }): Promise<ScheduledTask>;
  deleteTask(id: string): Promise<{ removed: boolean; id: string }>;
  analyzeAgent(agent: string): Promise<ImprovementReport>;
  getHistory(options?: { limit?: number; cursor?: string | null }): Promise<HistoryPage>;
};

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** `workspace` returns the workspace the PERSON selected (or null: their personal workspace). It is only a request: the server checks membership on every call. */
export function createApiClient(options: { baseUrl: string; tokens: TokenManager; fetchImpl?: FetchLike; workspace?: () => string | null }): ApiClient {
  const { baseUrl, tokens } = options;
  const doFetch: FetchLike = options.fetchImpl ?? ((input, init) => fetch(input, init));

  async function send(method: string, path: string, token: string, body?: unknown): Promise<Response> {
    const headers: Record<string, string> = { Accept: "application/json", Authorization: `Bearer ${token}` };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const selected = options.workspace?.();
    if (selected && UUID.test(selected) && !path.startsWith("/workspaces") && !path.startsWith("/invitations")) headers["X-Workspace-Id"] = selected; // the workspace routes choose or list workspaces themselves
    try {
      return await doFetch(`${baseUrl}${path}`, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        credentials: "omit",
        cache: "no-store",
      });
    } catch {
      throw new ApiError("network");
    }
  }

  async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
    let used = await tokens.get();
    let response = await send(method, path, used.token, body);
    if (response.status === 401) {
      // The backend rejects before executing anything, so one retry with a fresh token is safe for every method.
      tokens.invalidate(used.token);
      used = await tokens.get();
      response = await send(method, path, used.token, body);
    }
    if (!response.ok) {
      let detail: ErrorDetail | undefined;
      try {
        detail = sanitizeErrorBody(await response.json());
      } catch {
        detail = undefined;
      }
      throw new ApiError(errorKindForStatus(response.status), response.status, detail);
    }
    if (response.status === 204) return undefined as T;
    try {
      return (await response.json()) as T;
    } catch {
      throw new ApiError("server", response.status);
    }
  }

  const segment = (value: string) => encodeURIComponent(value);

  return {
    listTemplates: () => request("GET", "/agent-templates"),
    getTemplate: (templateId) => request("GET", `/agent-templates/${segment(templateId)}`),
    instantiateTemplate: (templateId, input) => request("POST", `/agent-templates/${segment(templateId)}/instantiate`, input ?? {}),
    listAgents: (status = "active") => request("GET", `/agents?status=${status}`),
    getAgent: (agentId) => request("GET", `/agents/${segment(agentId)}`),
    createAgent: (input) => request("POST", "/agents", input),
    updateAgent: (agentId, input) => request("PATCH", `/agents/${segment(agentId)}`, input),
    archiveAgent: (agentId) => request("POST", `/agents/${segment(agentId)}/archive`),
    unarchiveAgent: (agentId) => request("POST", `/agents/${segment(agentId)}/unarchive`),
    listVersions: (agentId) => request("GET", `/agents/${segment(agentId)}/versions`),
    getVersion: (agentId, version) => request("GET", `/agents/${segment(agentId)}/versions/${Number(version)}`),
    rollbackAgent: (agentId, input) => request("POST", `/agents/${segment(agentId)}/rollback`, input),
    createPlan: (objective) => request("POST", "/plans", { objective }),
    async listPlans({ status, limit, cursor } = {}) {
      const query = new URLSearchParams();
      if (status) query.set("status", status);
      if (limit !== undefined) query.set("limit", String(limit));
      if (cursor) query.set("cursor", cursor);
      const qs = query.toString();
      const page = await request<unknown>("GET", `/plans${qs ? `?${qs}` : ""}`);
      const { items, next_cursor: next } = (page ?? {}) as { items?: unknown; next_cursor?: unknown };
      if (!Array.isArray(items) || !(next === null || typeof next === "string")) throw new ApiError("server");
      return { items: items as PlanSummary[], next_cursor: next };
    },
    getPlan: (planId) => request("GET", `/plans/${segment(planId)}`),
    listRevisions: (planId) => request("GET", `/plans/${segment(planId)}/revisions`),
    getRevision: (planId, revision) => request("GET", `/plans/${segment(planId)}/revisions/${Number(revision)}`),
    validatePlan: (planId) => request("POST", `/plans/${segment(planId)}/validate`),
    revisePlan: (planId, input) => request("POST", `/plans/${segment(planId)}/revisions`, input),
    approvePlan: (planId, revision) => request("POST", `/plans/${segment(planId)}/approve`, { revision }),
    cancelPlan: (planId) => request("POST", `/plans/${segment(planId)}/cancel`),
    startRun: (planId) => request("POST", `/plans/${segment(planId)}/runs`),
    listRuns: (planId) => request("GET", `/plans/${segment(planId)}/runs`),
    getRun: (runId) => request("GET", `/plan-runs/${segment(runId)}`),
    getTaskRun: (runId, taskId) => request("GET", `/plan-runs/${segment(runId)}/tasks/${segment(taskId)}`),
    cancelRun: (runId) => request("POST", `/plan-runs/${segment(runId)}/cancel`),
    retryRun: (runId, input) => request("POST", `/plan-runs/${segment(runId)}/retry`, { acknowledge_uncertain: input?.acknowledge_uncertain ?? [] }),
    listTools: () => request("GET", "/tools"),
    getTool: (toolId) => request("GET", `/tools/${segment(toolId)}`),
    listToolInvocations({ runId, toolId, agent, status, limit, cursor } = {}) {
      const query = new URLSearchParams();
      if (runId) query.set("run_id", runId);
      if (toolId) query.set("tool_id", toolId);
      if (agent) query.set("agent", agent);
      if (status) query.set("status", status);
      if (limit !== undefined) query.set("limit", String(Math.trunc(limit)));
      if (cursor) query.set("cursor", cursor);
      const qs = query.toString();
      return request<ToolInvocationPage>("GET", `/tool-invocations${qs ? `?${qs}` : ""}`);
    },
    getToolInvocation: (invocationId) => request("GET", `/tool-invocations/${segment(invocationId)}`),
    getRunToolInvocations: (runId) => request("GET", `/plan-runs/${segment(runId)}/tool-invocations`),
    listApprovals({ status, runId, limit } = {}) {
      const query = new URLSearchParams();
      if (status) query.set("status", status);
      if (runId) query.set("run_id", runId);
      if (limit !== undefined) query.set("limit", String(Math.trunc(limit)));
      const qs = query.toString();
      return request<ApprovalList>("GET", `/approvals${qs ? `?${qs}` : ""}`);
    },
    getApproval: (approvalId) => request("GET", `/approvals/${segment(approvalId)}`),
    getDemo: () => request("GET", "/demo"),
    createStarterWorkforce: () => request("POST", "/starter-workforce"),
    decideApproval: (approvalId, decision, shownHash) => request("POST", `/approvals/${segment(approvalId)}/${decision}`, shownHash ? { payload_hash: shownHash } : {}),
    getRunEvents(runId, { after, limit } = {}) {
      const query = new URLSearchParams();
      if (after !== undefined) query.set("after", String(Math.trunc(after)));
      if (limit !== undefined) query.set("limit", String(Math.trunc(limit)));
      const qs = query.toString();
      return request<RunEventPage>("GET", `/plan-runs/${segment(runId)}/events${qs ? `?${qs}` : ""}`);
    },
    listWorkspaces: () => request("GET", "/workspaces"),
    createWorkspace: (name) => request("POST", "/workspaces", { name }),
    listWorkspaceMembers: (workspaceId) => request("GET", `/workspaces/${segment(workspaceId)}/members`),
    addWorkspaceMember: (workspaceId, invitee, role) => request("POST", `/workspaces/${segment(workspaceId)}/members`, { email: invitee, role }),
    inviteToWorkspace: (workspaceId, invitee, role) => request("POST", `/workspaces/${segment(workspaceId)}/invitations`, { email: invitee, role }),
    listInvitations: (workspaceId) => request("GET", `/workspaces/${segment(workspaceId)}/invitations`),
    revokeInvitation: (workspaceId, invitationId) => request("DELETE", `/workspaces/${segment(workspaceId)}/invitations/${segment(invitationId)}`),
    acceptInvitation: (token) => request("POST", "/invitations/accept", { token }),
    changeMemberRole: (workspaceId, member, role) => request("PATCH", `/workspaces/${segment(workspaceId)}/members`, { email: member, role }),
    removeMember: (workspaceId, member) => request("POST", `/workspaces/${segment(workspaceId)}/members/remove`, { email: member }),
    leaveWorkspace: (workspaceId) => request("POST", `/workspaces/${segment(workspaceId)}/leave`),
    listNotifications({ unread, limit } = {}) {
      const query = new URLSearchParams();
      if (unread) query.set("unread", "true");
      if (limit !== undefined) query.set("limit", String(Math.trunc(limit)));
      const qs = query.toString();
      return request<NotificationList>("GET", `/notifications${qs ? `?${qs}` : ""}`);
    },
    getNotificationCounts: () => request("GET", "/notifications/count"),
    markNotificationRead: (notificationId) => request("POST", `/notifications/${segment(notificationId)}/read`),
    markAllNotificationsRead: () => request("POST", "/notifications/read"),
    getRunUsage: (runId) => request("GET", `/plan-runs/${segment(runId)}/usage`),
    getRunProviderEvents: (runId) => request("GET", `/plan-runs/${segment(runId)}/provider-events`),
    listReconciliations: () => request("GET", "/reconciliations"),
    reconcileInvocation: (invocationId, input) => request("POST", `/tool-invocations/${segment(invocationId)}/reconcile`, input),
    runAutonomous: (agent, task) => request("POST", "/autonomous/run", { agent, task }),
    listTasks: () => request("GET", "/tasks"),
    scheduleTask: (input) => request("POST", "/tasks/schedule", input),
    deleteTask: (id) => request("DELETE", `/tasks/${segment(id)}`),
    analyzeAgent: (agent) => request("POST", `/improve/${segment(agent)}`),
    async getHistory({ limit, cursor } = {}) {
      const query = new URLSearchParams();
      if (limit !== undefined) query.set("limit", String(limit));
      if (cursor) query.set("cursor", cursor);
      const qs = query.toString();
      const page = await request<unknown>("GET", `/history${qs ? `?${qs}` : ""}`);
      const { items, next_cursor: next } = (page ?? {}) as { items?: unknown; next_cursor?: unknown };
      if (!Array.isArray(items) || !(next === null || typeof next === "string")) throw new ApiError("server");
      return { items: items as HistoryEntry[], next_cursor: next };
    },
  };
}

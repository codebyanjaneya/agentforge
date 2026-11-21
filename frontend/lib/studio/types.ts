// Wire contracts of the AgentForge Studio (M3-C). These mirror the backend's JSON exactly (M2-A agents and templates,
// M3-A plans, M3-B plan runs); nothing here is invented. The backend OpenAPI document is the source of truth and the
// contract test in tests/ compares the client against it.

export type AgentStatus = "active" | "archived";

export type ModelConfig = { provider: "groq"; name: string; temperature: number; max_tokens: number | null };

export type AgentDefinition = {
  name: string;
  description: string;
  role: string;
  purpose: string;
  instructions: string;
  capabilities: string[];
  allowed_tools: string[];
  input_expectations: string;
  output_expectations: string;
  model: ModelConfig;
  metadata: Record<string, string>;
};

export type TemplateSummary = { id: string; version: number; name: string; description: string; role: string; capabilities: string[] };
export type TemplateDetail = TemplateSummary & { definition: AgentDefinition };

export type VersionInfo = {
  version: number;
  content_hash: string;
  change_note: string | null;
  restored_from_version: number | null;
  created_at: string;
};
export type VersionDetail = VersionInfo & { definition: AgentDefinition };

export type AgentSummary = {
  id: string;
  slug: string;
  name: string;
  description: string;
  role: string;
  status: AgentStatus;
  current_version: number;
  latest_version: number;
  template_id: string | null;
  template_version: number | null;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
};
export type AgentDetail = AgentSummary & { definition: AgentDefinition; version: VersionInfo };

// ------------------------------------------------------------------------------------------------ plans (M3-A)

export type PlanStatus = "draft" | "approved" | "cancelled";
export type Complexity = "low" | "medium" | "high";
export type Priority = "low" | "normal" | "high";

export type PlanTaskAgent = { source: "builtin" | "custom"; key: string; name: string; version: number | null; version_id: string | null };
export type PlanTask = {
  id: string;
  position: number;
  level: number;
  title: string;
  description: string;
  expected_output: string;
  rationale: string;
  dependency_reason: string | null;
  depends_on: string[];
  complexity: Complexity;
  priority: Priority;
  agent: PlanTaskAgent;
  /** The exact tool this task MUST really invoke to count as done (server-resolved, e.g. "demo.send_note@1"); null when it requires no specific action. */
  required_tool: string | null;
};
export type PlanWarning = { code: string; task?: string | null; message: string };
export type Unsupported = { requirement: string; reason: string };
export type PlanRevision = {
  revision: number;
  origin: "model" | "revised";
  interpretation: string;
  warnings: PlanWarning[];
  unsupported: Unsupported[];
  provider: string | null;
  model: string | null;
  created_at: string;
  levels: string[][];
  tasks: PlanTask[];
};
export type LatestRun = { id: string; status: RunStatus; attempt: number; created_at: string; completed_at: string | null };
export type PlanDetail = {
  id: string;
  objective: string;
  status: PlanStatus;
  latest_revision: number;
  approved_revision: number | null;
  created_at: string;
  updated_at: string;
  approved_at: string | null;
  revision: PlanRevision;
  execution: { started: boolean };
  latest_run: LatestRun | null;
  created_by?: string | null;
};
export type PlanSummary = {
  id: string;
  objective: string;
  status: PlanStatus;
  latest_revision: number;
  approved_revision: number | null;
  task_count: number;
  created_by?: string | null;
  created_at: string;
  updated_at: string;
  approved_at: string | null;
};
export type PlanListFilter = "active" | "draft" | "approved" | "cancelled" | "all";
export type PlanPage = { items: PlanSummary[]; next_cursor: string | null };
export type RevisionListItem = { revision: number; origin: "model" | "revised"; created_at: string; provider: string | null; model: string | null };
export type ValidationReport = { valid: boolean; revision: number; status: PlanStatus; errors: { code: string; task: string | null; message: string }[]; warnings: PlanWarning[]; levels: string[][] };

/** The editable shape of a task in a revision request (rationale is optional on the wire). */
export type RevisedTaskInput = {
  id: string;
  title: string;
  description: string;
  expected_output: string;
  agent: string;
  depends_on: string[];
  dependency_reason?: string | null;
  complexity: Complexity;
  priority: Priority;
  rationale?: string;
  required_tool?: string | null;
};

// ------------------------------------------------------------------------------------------------ runs (M3-B)

export type RunStatus = "running" | "succeeded" | "partial" | "failed" | "cancelled";
export type TaskRunStatus = "pending" | "running" | "succeeded" | "failed" | "skipped" | "cancelled";
export type SafeError = { code: string; message: string | null };
export type TaskRun = {
  id: string;
  position: number;
  level: number;
  title: string;
  status: TaskRunStatus;
  depends_on: string[];
  blocked_by: string[];
  execution_id: string | null;
  agent: PlanTaskAgent;
  error: SafeError | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  /** M4-A2: set when the task was carried over from an earlier attempt (it was not executed again). */
  reused: { run_id: string; attempt: number } | null;
  /** M4-A2: for a task that did not succeed: "safe" (re-running cannot repeat an effect) or "uncertain" (the outcome is unknown). */
  retry: "safe" | "uncertain" | "reconcile" | null;
};
export type RunCounts = Record<TaskRunStatus | "total", number>;
export type RunOutput = { task: string; title: string; execution_id: string; result: string };
export type RunDetail = {
  id: string;
  plan_id: string;
  revision: number;
  attempt: number;
  started_by?: string | null;
  status: RunStatus;
  error: SafeError | null;
  counts: RunCounts;
  created_at: string;
  completed_at: string | null;
  tasks: TaskRun[];
  outputs: RunOutput[];
  retry_of: RunLink | null;
  retried_by: RunLink | null;
  /** Running runs only: whether a process currently owns the run. Never says which one. */
  ownership: "live" | "expired" | "unclaimed" | "none" | "suspended" | null;
  /** Runs that ended without success: whether an explicit retry is possible now and which tasks need an acknowledgement. */
  retry_options: { allowed: boolean; reason: string | null; uncertain: string[]; reconcile?: string[] } | null;
};
export type RunLink = { id: string; attempt: number };
export type RunEvent = {
  id: number;
  type: string;
  task: string | null;
  execution_id: string | null;
  actor: "user" | "api" | "worker" | "recovery";
  detail: Record<string, string | number | boolean | string[]>;
  created_at: string;
};
export type RunEventPage = { items: RunEvent[]; next_after: number | null };
export type RunListItem = {
  id: string;
  revision: number;
  attempt: number;
  status: RunStatus;
  error: { code: string } | null;
  counts: RunCounts;
  created_at: string;
  completed_at: string | null;
};
export type TaskRunDetail = TaskRun & {
  run_id: string;
  description: string;
  expected_output: string;
  execution: {
    id: string;
    status: string;
    provider: string | null;
    model: string | null;
    input: string;
    result: string | null;
    started_at: string;
    completed_at: string | null;
  } | null;
};

// ------------------------------------------------------------------------------------------------ tools (M4-B)

export type JsonSchema = Record<string, unknown>;
export type ToolRisk = "read" | "write" | "external";
export type ToolSideEffect = "none" | "idempotent" | "non_idempotent";
export type ToolLifecycle = "active" | "deprecated" | "disabled";
export type ToolDelivery = { retry_policy: string; remote_idempotency: boolean; can_query_remote: boolean; guarantee: string };
export type ToolSummary = {
  /** Present for tools with side effects: what is (and is not) guaranteed when the call is dispatched. */
  delivery?: ToolDelivery;
  id: string;
  version: number;
  ref: string;
  title: string;
  summary: string;
  category: string;
  capabilities: string[];
  risk: ToolRisk;
  side_effect: ToolSideEffect;
  lifecycle: ToolLifecycle;
  /** May this exact version run now (not disabled, and allowed by the policy: no approval path exists yet). */
  executable: boolean;
  policy: "allow" | "requires_approval" | "deny";
  /** M4-C: every call of this tool needs a human approval of that exact call. */
  requires_approval: boolean;
  /** M4-C: may an agent be given this tool (runnable now, or runnable with approvals). */
  grantable: boolean;
  timeout_seconds: number;
  max_output_bytes: number;
};
export type ToolUsage = { calls: number; last_used: string | null };
export type ToolGrant = { id: string; slug: string; name: string; version: number | null };
export type ToolCatalogItem = {
  id: string;
  title: string;
  summary: string;
  category: string;
  capabilities: string[];
  latest: ToolSummary;
  versions: { version: number; lifecycle: ToolLifecycle; executable: boolean }[];
  usage: ToolUsage;
  /** The caller's own agents whose current version is permitted to use this tool. */
  agents: ToolGrant[];
};
export type ToolCatalog = { items: ToolCatalogItem[]; policy: { max_risk: string; side_effects: string; approvals: string } };
export type ToolVersionDetail = ToolSummary & {
  description: string;
  limitations: string[];
  example: Record<string, unknown>;
  input_schema: JsonSchema;
  output_schema: JsonSchema;
  usage: ToolUsage;
};
export type InvocationStatus = "running" | "succeeded" | "failed" | "timed_out" | "rejected" | "cancelled" | "interrupted" | "uncertain";
export type ToolInvocationItem = {
  /** P0: the dispatch state machine of a side-effecting call (null for a tool without side effects). */
  dispatch?: DispatchView | null;
  id: string;
  execution_id: string;
  run_id: string | null;
  task: string | null;
  agent: string;
  tool: { id: string; version: number | null };
  seq: number;
  status: InvocationStatus;
  side_effect: ToolSideEffect;
  error: SafeError | null;
  created_at: string;
  started_at: string;
  completed_at: string | null;
  duration_ms: number | null;
  input_preview: string | null;
  output_preview: string | null;
};
export type ToolInvocationDetail = Omit<ToolInvocationItem, "input_preview" | "output_preview"> & {
  input: Record<string, unknown> | null;
  output: Record<string, unknown> | null;
  args_hash: string | null;
};
export type ToolDetail = { id: string; versions: ToolVersionDetail[]; agents: ToolGrant[]; recent_invocations: ToolInvocationItem[] };
export type ToolInvocationPage = { items: ToolInvocationItem[]; next_cursor: string | null };
export type RunToolInvocations = { items: ToolInvocationItem[]; truncated: boolean };

// ------------------------------------------------------------------------------------------------ M4-C: human approval of one exact tool call

export type ApprovalStatus = "pending" | "approved" | "rejected" | "expired" | "invalidated" | "consumed";
export type ApprovalItem = {
  id: string;
  status: ApprovalStatus;
  execution_id: string;
  run_id: string | null;
  task: string | null;
  agent: string;
  agent_version_id: string | null;
  seq: number;
  tool: { id: string; version: number; ref: string; title: string; summary: string | null; risk: string; side_effect: string; limitations: string[] };
  impact: string;
  /** Tools do not declare reversibility: it is reported as unknown, never guessed. */
  reversible: boolean | null;
  arguments: Record<string, unknown> | null;
  args_hash: string;
  payload_hash: string;
  /** The agent's own words (untrusted): shown as what the agent said, never as a fact. */
  reason: string | null;
  created_at: string;
  expires_at: string;
  decided_at: string | null;
  decided: boolean;
  consumed_at: string | null;
  invocation_id: string | null;
  /** The task is durably suspended on this approval right now: saved in the database, held by no process. */
  suspended: boolean;
  /** Only a workspace owner or admin may decide; a member can see the request. */
  can_decide: boolean;
  /** What an approver needs to judge a request that may belong to ANOTHER member's run (never an email). */
  context: { objective: string; started_by: "you" | "another member"; task_title: string | null } | null;
};
export type ApprovalList = { items: ApprovalItem[]; pending: number; can_decide: boolean };

// ------------------------------------------------------------------------------------------------ M5: demo status and the starter workforce

/** Whether this backend runs in DEMO MODE (a scripted model and a simulated sending tool; everything else is real). */
export type DemoInfo = { demo: boolean; model: "scripted" | "live"; model_name: string | null; live_tools: string[]; simulated_tools: string[]; example_objective: string; starter: string[] };
export type StarterAgent = { slug: string; name: string; role: string; tools: string[]; created: boolean };
export type StarterResult = { agents: StarterAgent[] };

// ------------------------------------------------------------------------------------------------ M6: workspaces, notifications and usage

export type WorkspaceRole = "owner" | "admin" | "member";
export type WorkspaceInfo = { id: string; name: string; personal: boolean; role: WorkspaceRole; members: number };
export type WorkspaceMember = { email: string; role: WorkspaceRole };
export type NotificationItem = {
  id: string;
  kind: "approval_requested";
  approval_id: string;
  run_id: string | null;
  created_at: string;
  read_at: string | null;
  approval: { status: ApprovalStatus; tool: string; agent: string; task: string | null; expires_at: string };
};
/** `pending` is the inbox badge (requests still waiting for a decision); `unread` counts notifications not yet marked read. */
export type NotificationCounts = { pending: number; unread: number };
export type NotificationList = NotificationCounts & { items: NotificationItem[] };

export type UsageSummary = {
  requests: number;
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number;
  requests_without_usage: number;
  /** Micro-dollars (1e-6 USD), null when no request had a calculable cost. */
  cost_micros: number | null;
  /** True only when EVERY request has a calculated cost. */
  cost_complete: boolean;
  requests_without_price: number;
};
export type RunUsage = {
  totals: UsageSummary;
  planning: UsageSummary | null;
  agents: UsageSummary | null;
  by_model: (UsageSummary & { provider: string; model: string })[];
  by_task: (UsageSummary & { task: string | null; agent: string | null })[];
  pricing_versions: string[];
  pricing: { current_version: string; unit: string; note: string };
};

// ------------------------------------------------------------------------------------------------ M6b: provider retries

export type ProviderEventKind = "waiting" | "retrying" | "recovered" | "exhausted" | "failed";
export type ProviderEvent = { task: string | null; kind: ProviderEventKind; attempt: number; error_code: string | null; wait_ms: number | null; detail: string | null; provider: string; at: string };
export type ProviderTaskState = { state: ProviderEventKind; attempt: number; error_code: string | null; wait_ms: number | null };
export type RunProviderEvents = { items: ProviderEvent[]; by_task: Record<string, ProviderTaskState> };

// ------------------------------------------------------------------------------------------------ P0: durable dispatch and reconciliation

export type DispatchState = "authorized" | "dispatching" | "succeeded" | "failed" | "unknown" | "reconciled_succeeded" | "reconciled_failed" | "cancelled";
export type DispatchView = {
  id: string;
  state: DispatchState;
  generation: number;
  idempotency_key: string;
  external_ref: string | null;
  dispatch_started_at: string | null;
  completed_at: string | null;
  /** confirmed (we know) | unknown (we do not) | reconciled (decided through an audited record) | not_sent | in_flight */
  outcome: "confirmed" | "unknown" | "reconciled" | "not_sent" | "in_flight";
  /** true / false when KNOWN; null while it is unknown or in flight: never guessed. */
  performed: boolean | null;
  requires_reconciliation: boolean;
  reconciliation: { decision: "confirmed_succeeded" | "confirmed_failed" | "unresolved"; evidence_kind: "remote_query" | "operator_attestation"; evidence: string; reason: string; at: string; attestation_only: boolean; by: string | null; attempts: number } | null;
};
export type ReconciliationList = { can_reconcile: boolean; items: { invocation_id: string; run_id: string | null; task: string | null; tool: string; dispatch: DispatchView }[] };
export type ReconcileInput =
  | { mode: "query"; reason: string }
  | { mode: "attest"; decision: "confirmed_succeeded" | "confirmed_failed"; evidence: string; reason: string };
export type JoinedWorkspace = { workspace_id: string; name: string; role: WorkspaceRole };
export type WorkspaceInvitation = { id: string; email: string; role: Exclude<WorkspaceRole, "owner">; expires_at: string };
export type WorkspaceInvitationCreated = WorkspaceInvitation & { token: string };

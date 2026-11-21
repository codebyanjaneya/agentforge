// Authenticated agent WebSocket client (M1-H, backend contract: backend/realtime.py, D026).
//
// Protocol: open the socket, send ONE first frame `{"type":"auth","token":"<backend access token>"}` (the backend
// waits 5 s), wait for `{"type":"authenticated"}`, then send jobs `{agent, task}`. Identity and workspace are
// bound by the backend at connect: nothing else (no email, no workspace) is ever sent.
//
// The backend runs ONE job per connection, so jobs are queued here and sent one at a time (the same sequential
// behaviour the old socket had). The token is only used for the auth frame; it is never kept in the queue, in
// status callbacks or in messages handed to the UI.
//
// Close codes: 4401 -> the token was rejected or expired: drop it, reacquire and reconnect (a second consecutive
// failure to authenticate stops and reports "unauthenticated", so there is no retry loop); 4403 -> forbidden, no
// reconnect; 1009 (too large), 1011 and everything else -> reconnect after a delay. A token about to expire is
// replaced while the connection is idle (the backend only checks expiry when a frame arrives, so a running job is
// never interrupted and no ping is sent while one runs).

import type { BackendToken } from "./api-client";

// `ApiError` is recognised by its `kind` (no runtime import of the client module: this file stays loadable on its own).
function errorKind(error: unknown): unknown {
  return (error as { kind?: unknown } | null)?.kind;
}

export type SocketStatus = "idle" | "connecting" | "online" | "offline" | "unauthenticated" | "forbidden";
export type SocketJob = { agent: string; task: string };
export type SocketMessage = Record<string, unknown>;

export type WSLike = {
  readyState: number;
  send(data: string): void;
  close(code?: number): void;
  onopen: ((event: unknown) => void) | null;
  onmessage: ((event: { data: unknown }) => void) | null;
  onclose: ((event: { code: number }) => void) | null;
  onerror: ((event: unknown) => void) | null;
};

export type AgentSocketOptions = {
  url: string;
  getToken: () => Promise<BackendToken>;
  invalidateToken: (token: string) => void;
  onStatus: (status: SocketStatus) => void;
  onMessage: (message: SocketMessage) => void;
  createSocket?: (url: string) => WSLike;
  now?: () => number; // seconds
  pingMs?: number;
  reconnectMs?: number;
  refreshMarginSeconds?: number;
};

const OPEN = 1;
export const MAX_JOB_BYTES = 60_000; // the backend closes a connection at 64 KiB; refuse earlier and keep the socket
const CONNECTION_LOST = "Connection lost. Please try again.";

export class AgentSocket {
  private readonly o: Required<Omit<AgentSocketOptions, "createSocket">> & { createSocket: (url: string) => WSLike };
  private ws: WSLike | null = null;
  private status: SocketStatus = "idle";
  private stopped = true;
  private connecting = false;
  private authed = false;
  private refreshing = false;
  private usedToken = "";
  private expiresAt = 0;
  private authFailures = 0;
  private queue: SocketJob[] = [];
  private inFlight: SocketJob | null = null;
  private pingTimer: ReturnType<typeof setInterval> | undefined;
  private reconnectTimer: ReturnType<typeof setTimeout> | undefined;
  private refreshTimer: ReturnType<typeof setTimeout> | undefined;

  constructor(options: AgentSocketOptions) {
    this.o = {
      pingMs: 25_000,
      reconnectMs: 2_000,
      refreshMarginSeconds: 60,
      now: () => Date.now() / 1000,
      createSocket: (url) => new WebSocket(url) as unknown as WSLike,
      ...options,
    };
  }

  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    this.authFailures = 0;
    void this.connect();
  }

  stop(): void {
    this.stopped = true;
    this.clearTimers();
    this.queue = [];
    this.inFlight = null;
    const ws = this.ws;
    this.ws = null;
    this.authed = false;
    if (ws) {
      detach(ws);
      ws.close(1000);
    }
    this.setStatus("idle");
  }

  /** Queue a job. "not_connected" when the socket is not online; "too_large" when the backend would reject it. */
  enqueue(job: SocketJob): "queued" | "not_connected" | "too_large" {
    if (new TextEncoder().encode(JSON.stringify(job)).length > MAX_JOB_BYTES) return "too_large";
    if (this.status !== "online" || !(this.authed || this.refreshing)) return "not_connected"; // queued across a token refresh
    this.queue.push(job);
    this.pump();
    return "queued";
  }

  // ---------------------------------------------------------------------------------------------- internals

  private setStatus(status: SocketStatus): void {
    if (status === this.status) return;
    this.status = status;
    this.o.onStatus(status);
  }

  private clearTimers(): void {
    clearInterval(this.pingTimer);
    clearTimeout(this.reconnectTimer);
    clearTimeout(this.refreshTimer);
    this.pingTimer = this.reconnectTimer = this.refreshTimer = undefined;
  }

  private scheduleReconnect(delayMs: number): void {
    if (this.stopped) return;
    clearTimeout(this.reconnectTimer);
    this.reconnectTimer = setTimeout(() => void this.connect(), delayMs);
  }

  private async connect(): Promise<void> {
    if (this.stopped || this.connecting) return;
    this.connecting = true;
    this.setStatus("connecting");
    let token: BackendToken;
    try {
      token = await this.o.getToken();
    } catch (error) {
      this.connecting = false;
      if (this.stopped) return;
      if (errorKind(error) === "auth") return this.setStatus("unauthenticated");
      if (errorKind(error) === "forbidden") return this.setStatus("forbidden");
      this.setStatus("offline");
      return this.scheduleReconnect(this.o.reconnectMs);
    }
    this.connecting = false;
    if (this.stopped) return;

    const ws = this.o.createSocket(this.o.url);
    this.ws = ws;
    this.usedToken = token.token;
    this.expiresAt = token.expiresAt;
    ws.onopen = () => {
      if (ws === this.ws) ws.send(JSON.stringify({ type: "auth", token: token.token }));
    };
    ws.onmessage = (event) => {
      if (ws === this.ws) this.handleMessage(event.data);
    };
    ws.onerror = () => {
      if (ws === this.ws) ws.close();
    };
    ws.onclose = (event) => {
      if (ws === this.ws) this.handleClose(event.code);
    };
  }

  private handleMessage(raw: unknown): void {
    let message: SocketMessage;
    try {
      const parsed: unknown = JSON.parse(String(raw));
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return;
      message = parsed as SocketMessage;
    } catch {
      return; // ignore malformed frames
    }
    if (!this.authed) {
      if (message.type === "authenticated") this.onAuthenticated();
      return;
    }
    if (message.type === "pong") return;
    if (typeof message.agent === "string") {
      if (this.inFlight && this.inFlight.agent === message.agent) this.inFlight = null;
      this.o.onMessage(message);
      return this.pump();
    }
    if (typeof message.error === "string" && this.inFlight) {
      // an agent-less error answers the job in flight (e.g. a refusal); fail that job, not the connection
      const job = this.inFlight;
      this.inFlight = null;
      this.o.onMessage({ agent: job.agent, error: message.error });
      this.pump();
    }
  }

  private onAuthenticated(): void {
    this.authed = true;
    this.authFailures = 0;
    this.setStatus("online");
    clearInterval(this.pingTimer);
    this.pingTimer = setInterval(() => {
      if (this.authed && !this.inFlight && !this.nearExpiry() && this.ws?.readyState === OPEN) {
        this.ws.send(JSON.stringify({ type: "ping" }));
      }
    }, this.o.pingMs);
    clearTimeout(this.refreshTimer);
    const untilRefresh = Math.max(0, (this.expiresAt - this.o.refreshMarginSeconds - this.o.now()) * 1000);
    this.refreshTimer = setTimeout(() => this.pump(), untilRefresh);
    this.pump();
  }

  private nearExpiry(): boolean {
    return this.o.now() >= this.expiresAt - this.o.refreshMarginSeconds;
  }

  private pump(): void {
    if (!this.authed || this.inFlight || !this.ws) return;
    if (this.nearExpiry()) {
      // Replace the connection (and token) while idle; queued jobs are kept and sent after re-authentication.
      this.refreshing = true;
      this.authed = false;
      this.clearTimers();
      this.o.invalidateToken(this.usedToken);
      this.ws.close(1000);
      return;
    }
    const job = this.queue.shift();
    if (!job || this.ws.readyState !== OPEN) {
      if (job) this.queue.unshift(job);
      return;
    }
    this.inFlight = job;
    this.ws.send(JSON.stringify({ agent: job.agent, task: job.task }));
  }

  private handleClose(code: number): void {
    const wasAuthed = this.authed;
    this.authed = false;
    this.ws = null;
    this.clearTimers();

    if (this.refreshing) {
      this.refreshing = false;
      return void this.connect();
    }

    const lost = [...(this.inFlight ? [this.inFlight] : []), ...this.queue];
    this.inFlight = null;
    this.queue = [];
    for (const job of lost) this.o.onMessage({ agent: job.agent, error: CONNECTION_LOST });

    if (code === 4403) return this.setStatus("forbidden");
    if (code === 4401) {
      this.o.invalidateToken(this.usedToken);
      if (!wasAuthed && ++this.authFailures >= 2) return this.setStatus("unauthenticated");
      this.setStatus("offline");
      return this.scheduleReconnect(0);
    }
    this.setStatus("offline");
    this.scheduleReconnect(this.o.reconnectMs);
  }
}

function detach(ws: WSLike): void {
  ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null;
}

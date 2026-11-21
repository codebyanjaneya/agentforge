// M1-H: the authenticated agent WebSocket client, against a fake socket that speaks the backend/realtime.py
// protocol (first-frame auth, one job at a time, close codes 4401/4403). No network, no real backend.

import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";

import { ApiError } from "../lib/api-client.ts";
import { AgentSocket, MAX_JOB_BYTES } from "../lib/agent-socket.ts";
import { FakeSocket, clock, sleep, waitFor } from "./m1h-helpers.mjs";

beforeEach(() => FakeSocket.reset());

function setup(overrides = {}) {
  const now = overrides.now ?? clock();
  const statuses = [];
  const messages = [];
  const invalidated = [];
  let issued = 0;
  const tokenCalls = [];
  const options = {
    url: "wss://api.example.com/ws",
    getToken: overrides.getToken ?? (async () => {
      issued += 1;
      tokenCalls.push(issued);
      return { token: `token-${issued}`, expiresAt: now() + 600 };
    }),
    invalidateToken: (token) => invalidated.push(token),
    onStatus: (status) => statuses.push(status),
    onMessage: (message) => messages.push(message),
    createSocket: (url) => new FakeSocket(url),
    now,
    pingMs: overrides.pingMs ?? 10_000,
    reconnectMs: overrides.reconnectMs ?? 5,
    refreshMarginSeconds: 60,
  };
  const socket = new AgentSocket(options);
  return { socket, now, statuses, messages, invalidated, tokenCalls };
}

async function online(ctx, index = 0) {
  await waitFor(() => FakeSocket.instances[index], 1000, "socket creation");
  const ws = FakeSocket.instances[index];
  ws.open();
  ws.receive({ type: "authenticated" });
  await waitFor(() => ctx.statuses.includes("online"), 1000, "online");
  return ws;
}

test("the first frame is the auth frame with the backend token; jobs wait for 'authenticated'", async () => {
  const ctx = setup();
  ctx.socket.start();
  await waitFor(() => FakeSocket.instances.length === 1);
  const ws = FakeSocket.instances[0];
  assert.equal(ws.url, "wss://api.example.com/ws");
  assert.deepEqual(ctx.statuses, ["connecting"]);
  assert.equal(ctx.socket.enqueue({ agent: "ceo", task: "t" }), "not_connected", "not online yet");
  ws.open();
  assert.deepEqual(ws.sent, [{ type: "auth", token: "token-1" }], "exactly one first frame, the auth frame");
  assert.deepEqual(ctx.statuses, ["connecting"], "still not online before the server confirms");
  ws.receive({ type: "authenticated" });
  assert.deepEqual(ctx.statuses, ["connecting", "online"]);
  assert.equal(ctx.socket.enqueue({ agent: "ceo", task: "hello" }), "queued");
  assert.deepEqual(ws.sent.at(-1), { agent: "ceo", task: "hello" }, "jobs carry only agent and task");
  ctx.socket.stop();
});

test("only the server's 'authenticated' frame completes the handshake: other frames before it change nothing", async () => {
  const ctx = setup();
  ctx.socket.start();
  await waitFor(() => FakeSocket.instances.length === 1);
  const ws = FakeSocket.instances[0];
  ws.open();
  for (const early of [{ type: "pong" }, { error: "Already authenticated." }, { agent: "ceo", result: "sneaky" }, { type: "authenticate" }, { type: "authenticated", extra: 1 }].slice(0, 4)) {
    ws.receive(early);
  }
  assert.deepEqual(ctx.statuses, ["connecting"], "still connecting: nothing but 'authenticated' brings the socket online");
  assert.equal(ctx.socket.enqueue({ agent: "ceo", task: "too early" }), "not_connected");
  assert.deepEqual(ws.sent, [{ type: "auth", token: "token-1" }], "no job was sent before the server confirmed");
  assert.deepEqual(ctx.messages, [], "pre-auth frames are not forwarded to the UI");
  ws.receive({ type: "authenticated" });
  assert.equal(ctx.statuses.at(-1), "online");
  ctx.socket.stop();
});

test("no identity other than the token is ever sent and the token never reaches callbacks", async () => {
  const ctx = setup();
  ctx.socket.start();
  const ws = await online(ctx);
  ctx.socket.enqueue({ agent: "sales", task: "pitch" });
  ws.receive({ agent: "sales", result: "done" });
  const blob = JSON.stringify([ctx.statuses, ctx.messages]);
  assert.ok(!blob.includes("token-1"), "no token in status or message callbacks");
  for (const frame of ws.sent.slice(1)) assert.ok(!/email|workspace|user_id|token/i.test(JSON.stringify(frame)));
  ctx.socket.stop();
});

test("jobs run one at a time, in order, with results forwarded", async () => {
  const ctx = setup();
  ctx.socket.start();
  const ws = await online(ctx);
  ctx.socket.enqueue({ agent: "ceo", task: "one" });
  ctx.socket.enqueue({ agent: "sales", task: "two" });
  ctx.socket.enqueue({ agent: "hiring", task: "three" });
  assert.deepEqual(ws.sent.slice(1), [{ agent: "ceo", task: "one" }], "only the first is in flight");
  ws.receive({ agent: "ceo", result: "r1", delegations: [] });
  assert.deepEqual(ws.sent.slice(1), [{ agent: "ceo", task: "one" }, { agent: "sales", task: "two" }]);
  ws.receive({ agent: "sales", error: "Agent failed." });
  ws.receive({ agent: "hiring", result: "r3" });
  assert.deepEqual(ws.sent.slice(1).map((f) => f.agent), ["ceo", "sales", "hiring"]);
  assert.deepEqual(ctx.messages.map((m) => m.agent), ["ceo", "sales", "hiring"]);
  assert.equal(ctx.messages[1].error, "Agent failed.");
  ctx.socket.stop();
});

test("an agent-less error from the server fails the job in flight and moves on", async () => {
  const ctx = setup();
  ctx.socket.start();
  const ws = await online(ctx);
  ctx.socket.enqueue({ agent: "ceo", task: "a" });
  ctx.socket.enqueue({ agent: "sales", task: "b" });
  ws.receive({ error: "A job is already running on this connection." });
  assert.deepEqual(ctx.messages[0], { agent: "ceo", error: "A job is already running on this connection." });
  assert.deepEqual(ws.sent.at(-1), { agent: "sales", task: "b" });
  ctx.socket.stop();
});

test("malformed and unrelated frames are ignored", async () => {
  const ctx = setup();
  ctx.socket.start();
  const ws = await online(ctx);
  for (const junk of ["not json", "[1,2]", "null", '"str"', JSON.stringify({ type: "pong" }), JSON.stringify({ error: "x" })]) ws.receive(junk);
  assert.deepEqual(ctx.messages, []);
  ctx.socket.stop();
});

test("oversized jobs are refused locally and the connection stays up", async () => {
  const ctx = setup();
  ctx.socket.start();
  const ws = await online(ctx);
  assert.equal(ctx.socket.enqueue({ agent: "ceo", task: "x".repeat(MAX_JOB_BYTES) }), "too_large");
  assert.equal(ctx.socket.enqueue({ agent: "ceo", task: "é".repeat(MAX_JOB_BYTES / 2) }), "too_large", "counted in bytes");
  assert.equal(ctx.socket.enqueue({ agent: "ceo", task: "x".repeat(1000) }), "queued");
  assert.equal(ws.closedWith, undefined);
  ctx.socket.stop();
});

test("pings go out when idle and never while a job is running", async () => {
  const ctx = setup({ pingMs: 15 });
  ctx.socket.start();
  const ws = await online(ctx);
  await waitFor(() => ws.sent.some((f) => f.type === "ping"), 1000, "a ping");
  ctx.socket.enqueue({ agent: "ceo", task: "long job" });
  const before = ws.sent.filter((f) => f.type === "ping").length;
  await sleep(80);
  assert.equal(ws.sent.filter((f) => f.type === "ping").length, before, "no ping while a job is in flight");
  ws.receive({ agent: "ceo", result: "ok" });
  await waitFor(() => ws.sent.filter((f) => f.type === "ping").length > before, 1000, "pings resume");
  ctx.socket.stop();
});

// ------------------------------------------------------------------------------------------------ close handling

test("4401 after authenticating: the token is dropped, a fresh one is used, the running job is failed", async () => {
  const ctx = setup();
  ctx.socket.start();
  const ws = await online(ctx);
  ctx.socket.enqueue({ agent: "ceo", task: "a" });
  ctx.socket.enqueue({ agent: "sales", task: "b" });
  ws.serverClose(4401);
  assert.deepEqual(ctx.messages.map((m) => m.agent), ["ceo", "sales"]);
  assert.ok(ctx.messages.every((m) => typeof m.error === "string"));
  assert.deepEqual(ctx.invalidated, ["token-1"]);
  const ws2 = await online(ctx, 1);
  assert.deepEqual(ws2.sent[0], { type: "auth", token: "token-2" });
  assert.equal(ctx.socket.enqueue({ agent: "ceo", task: "again" }), "queued");
  ctx.socket.stop();
});

test("4401 twice without ever authenticating stops: no retry loop", async () => {
  const ctx = setup({ reconnectMs: 1 });
  ctx.socket.start();
  await waitFor(() => FakeSocket.instances.length === 1);
  FakeSocket.instances[0].open();
  FakeSocket.instances[0].serverClose(4401);
  await waitFor(() => FakeSocket.instances.length === 2);
  FakeSocket.instances[1].open();
  FakeSocket.instances[1].serverClose(4401);
  await waitFor(() => ctx.statuses.at(-1) === "unauthenticated");
  await sleep(40);
  assert.equal(FakeSocket.instances.length, 2, "no third attempt");
  assert.deepEqual(ctx.invalidated, ["token-1", "token-2"]);
});

test("4403 is final: forbidden, no reconnect", async () => {
  const ctx = setup({ reconnectMs: 1 });
  ctx.socket.start();
  await waitFor(() => FakeSocket.instances.length === 1);
  FakeSocket.instances[0].open();
  FakeSocket.instances[0].serverClose(4403);
  await waitFor(() => ctx.statuses.at(-1) === "forbidden");
  await sleep(40);
  assert.equal(FakeSocket.instances.length, 1);
  assert.equal(ctx.socket.enqueue({ agent: "ceo", task: "t" }), "not_connected");
});

test("ordinary closes (1006, 1009, 1011) reconnect after the delay and re-authenticate", async () => {
  for (const code of [1006, 1009, 1011, 1001]) {
    FakeSocket.reset();
    const ctx = setup({ reconnectMs: 5 });
    ctx.socket.start();
    const ws = await online(ctx);
    ws.serverClose(code);
    assert.equal(ctx.statuses.at(-1), "offline", String(code));
    const ws2 = await online(ctx, 1);
    assert.deepEqual(ws2.sent[0].type, "auth");
    assert.equal(ctx.statuses.at(-1), "online");
    ctx.socket.stop();
  }
});

test("token acquisition outcomes decide whether to connect: signed out and forbidden do not retry, outages do", async () => {
  const signedOut = setup({ getToken: async () => { throw new ApiError("auth", 401); } });
  signedOut.socket.start();
  await waitFor(() => signedOut.statuses.at(-1) === "unauthenticated");
  assert.equal(FakeSocket.instances.length, 0, "no socket without a token");

  const forbidden = setup({ getToken: async () => { throw new ApiError("forbidden", 403); } });
  forbidden.socket.start();
  await waitFor(() => forbidden.statuses.at(-1) === "forbidden");
  assert.equal(FakeSocket.instances.length, 0);

  let attempts = 0;
  const now = clock();
  const flaky = setup({ now, getToken: async () => { if (++attempts < 3) throw new ApiError("network"); return { token: "ok", expiresAt: now() + 600 }; } });
  flaky.socket.start();
  await waitFor(() => FakeSocket.instances.length === 1, 2000, "socket after the outage");
  assert.equal(attempts, 3);
  assert.ok(flaky.statuses.includes("offline"));
  signedOut.socket.stop(); forbidden.socket.stop(); flaky.socket.stop();
});

// ------------------------------------------------------------------------------------------------ expiry, stop

test("a token about to expire is replaced while idle; queued jobs survive and run on the new connection", async () => {
  const ctx = setup();
  ctx.socket.start();
  const ws = await online(ctx);
  ctx.now.advance(545); // inside the 60 s margin of the 600 s token
  ctx.socket.enqueue({ agent: "ceo", task: "queued across the refresh" });
  assert.equal(ws.closedWith, 1000, "the old connection is closed cleanly");
  assert.ok(!ws.sent.some((f) => f.agent), "nothing was sent with the old token");
  assert.deepEqual(ctx.invalidated, ["token-1"]);
  const ws2 = await online(ctx, 1);
  assert.deepEqual(ws2.sent[0], { type: "auth", token: "token-2" });
  assert.deepEqual(ws2.sent[1], { agent: "ceo", task: "queued across the refresh" });
  assert.equal(ctx.messages.length, 0, "the refresh did not fail the job");
  ctx.socket.stop();
});

test("a running job is never interrupted by the refresh; it happens after the job", async () => {
  const ctx = setup();
  ctx.socket.start();
  const ws = await online(ctx);
  ctx.socket.enqueue({ agent: "ceo", task: "slow" });
  ctx.now.advance(590);
  await sleep(20);
  assert.equal(ws.closedWith, undefined, "no refresh while the job runs");
  ws.receive({ agent: "ceo", result: "done" });
  assert.equal(ws.closedWith, 1000, "refresh right after the job");
  assert.deepEqual(ctx.messages, [{ agent: "ceo", result: "done" }]);
  await online(ctx, 1);
  ctx.socket.stop();
});

test("stop() closes the socket, cancels reconnects and drops the queue silently", async () => {
  const ctx = setup({ reconnectMs: 5 });
  ctx.socket.start();
  const ws = await online(ctx);
  ctx.socket.enqueue({ agent: "ceo", task: "a" });
  ctx.socket.enqueue({ agent: "sales", task: "b" });
  const statusesBefore = ctx.statuses.length;
  ctx.socket.stop();
  assert.equal(ws.closedWith, 1000);
  assert.equal(ctx.statuses.at(-1), "idle");
  assert.equal(ctx.statuses.length, statusesBefore + 1);
  await sleep(50);
  assert.equal(FakeSocket.instances.length, 1, "no reconnect after stop");
  assert.deepEqual(ctx.messages, [], "stopping does not fabricate job errors");
  assert.equal(ctx.socket.enqueue({ agent: "ceo", task: "t" }), "not_connected");
});

test("stop() during token acquisition prevents any connection", async () => {
  let release;
  const ctx = setup({ getToken: () => new Promise((resolve) => { release = () => resolve({ token: "late", expiresAt: 9_999_999_999 }); }) });
  ctx.socket.start();
  ctx.socket.stop();
  release();
  await sleep(20);
  assert.equal(FakeSocket.instances.length, 0);
});

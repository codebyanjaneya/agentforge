// A single, cancellable polling loop (M3-C). Guarantees: at most ONE request in flight and ONE timer per poller (starting
// twice is a no-op), a tick never starts while the previous one is unfinished, the loop pauses while the tab is hidden,
// stops at a terminal state, backs off on transient errors, stops on errors retrying cannot fix (auth, forbidden, not
// found), and a stopped poller can never deliver a late result (generation check + abort).

import { ApiError } from "../api-client.ts";

export type PollerOptions<T> = {
  fetch: (signal: AbortSignal) => Promise<T>;
  onData: (value: T) => void;
  onError?: (error: unknown, info: { consecutive: number; willRetry: boolean }) => void;
  isTerminal: (value: T) => boolean;
  intervalMs?: number;
  maxIntervalMs?: number;
  maxConsecutiveErrors?: number;
  isVisible?: () => boolean;
  setTimer?: (fn: () => void, ms: number) => unknown;
  clearTimer?: (handle: unknown) => void;
};

export type Poller = { start(): void; stop(): void; refresh(): void; readonly running: boolean };

const FATAL = new Set(["auth", "forbidden", "not_found", "config"]);

export function createPoller<T>(options: PollerOptions<T>): Poller {
  const interval = options.intervalMs ?? 2000;
  const maxInterval = options.maxIntervalMs ?? 30000;
  const maxErrors = options.maxConsecutiveErrors ?? 8;
  const visible = options.isVisible ?? (() => typeof document === "undefined" || document.visibilityState !== "hidden");
  const setTimer = options.setTimer ?? ((fn, ms) => setTimeout(fn, ms));
  const clearTimer = options.clearTimer ?? ((handle) => clearTimeout(handle as ReturnType<typeof setTimeout>));

  let generation = 0;
  let running = false;
  let inflight = false;
  let timer: unknown = null;
  let consecutive = 0;
  let controller: AbortController | null = null;

  function schedule(ms: number, gen: number) {
    if (timer !== null) clearTimer(timer);
    timer = setTimer(() => {
      timer = null;
      void tick(gen);
    }, ms);
  }

  async function tick(gen: number) {
    if (gen !== generation || !running || inflight) return;
    if (!visible()) return schedule(interval, gen); // paused: no request while the tab is hidden
    inflight = true;
    controller = new AbortController();
    try {
      const value = await options.fetch(controller.signal);
      if (gen !== generation) return;
      consecutive = 0;
      options.onData(value);
      if (options.isTerminal(value)) {
        running = false;
        return;
      }
      schedule(interval, gen);
    } catch (error) {
      if (gen !== generation) return;
      consecutive += 1;
      const fatal = error instanceof ApiError && FATAL.has(error.kind);
      const willRetry = !fatal && consecutive < maxErrors;
      options.onError?.(error, { consecutive, willRetry });
      if (!willRetry) {
        running = false;
        return;
      }
      schedule(Math.min(maxInterval, interval * 2 ** consecutive), gen);
    } finally {
      if (gen === generation) inflight = false;
    }
  }

  const self: Poller = {
    start() {
      if (running) return;
      running = true;
      consecutive = 0;
      generation += 1;
      void tick(generation);
    },
    stop() {
      running = false;
      generation += 1; // late results of an in-flight request are ignored
      inflight = false;
      if (timer !== null) clearTimer(timer);
      timer = null;
      controller?.abort();
      controller = null;
    },
    refresh() {
      if (!running) {
        self.start();
        return;
      }
      if (inflight) return; // never a second request while one is pending
      if (timer !== null) clearTimer(timer);
      timer = null;
      void tick(generation);
    },
    get running() {
      return running;
    },
  };
  return self;
}

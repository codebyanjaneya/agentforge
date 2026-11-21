// Pure formatting for model usage and cost (M6). The backend calculates everything; this only words it, and never turns a missing number into a zero.

import type { UsageSummary } from "./types.ts";

/** Micro-dollars as dollars: at least four decimals below one cent so tiny real amounts are not shown as $0.00. */
export function formatCost(micros: number): string {
  if (micros === 0) return "$0";
  const dollars = micros / 1_000_000;
  return dollars >= 0.01 ? `$${dollars.toFixed(2)}` : `$${dollars.toFixed(6).replace(/0+$/, "").replace(/\.$/, "")}`;
}

export const tokens = (n: number): string => n.toLocaleString("en-US");

/** The cost cell: a calculated amount, an amount that is a LOWER BOUND (some requests had no usage or price), or an honest "not available". */
export function costLabel(s: UsageSummary): { text: string; note: string | null } {
  if (s.requests === 0) return { text: "No requests", note: null };
  if (s.cost_micros === null) return { text: "Not available", note: s.requests_without_usage ? "The provider reported no token counts." : "No price is configured for this model." };
  if (s.cost_complete) return { text: formatCost(s.cost_micros), note: null };
  const parts = [];
  if (s.requests_without_usage) parts.push(`${s.requests_without_usage} request${s.requests_without_usage === 1 ? "" : "s"} reported no token counts`);
  if (s.requests_without_price) parts.push(`${s.requests_without_price} used a model with no configured price`);
  return { text: `at least ${formatCost(s.cost_micros)}`, note: `Incomplete: ${parts.join("; ")}.` };
}

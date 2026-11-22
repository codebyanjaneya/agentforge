// Seeds a realistic workspace through the real API: starter workforce, plans, one completed run, one run waiting on an approval, one rejected. Scripted model, simulated send.
import fs from "node:fs";
import path from "node:path";
import { api, OUT } from "./lib.mjs";
const { workspace: ws } = JSON.parse(fs.readFileSync(path.join(OUT, "seed.json"), "utf8"));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const OBJ = "Prepare the launch of our new plan: price $49/month, 80% margin, 3% monthly churn, CAC $400. Email sam@acme.io on the launch date.";
async function journey(who, decideWith, decision) {
  const w = await api(who, "POST", "/starter-workforce", {}, ws);
  const p = await api(who, "POST", "/plans", { objective: OBJ }, ws); if (p.status !== 201) throw new Error("plan " + JSON.stringify(p));
  const id = p.body.id ?? p.body.plan?.id;
  const a = await api(who, "POST", `/plans/${id}/approve`, { revision: 1 }, ws); if (a.status >= 300) throw new Error("approve " + JSON.stringify(a));
  const r = await api(who, "POST", `/plans/${id}/runs`, {}, ws); if (r.status !== 202) throw new Error("run " + JSON.stringify(r));
  const run = r.body.id ?? r.body.run?.id;
  let appr;
  for (let i = 0; i < 60 && !appr; i++) { await sleep(1000); const l = await api(decideWith, "GET", "/approvals?status=pending", null, ws); appr = (l.body.items || []).find((x) => x.run_id === run); }
  if (!decision) return { plan: id, run, approval: appr?.id };
  const d = await api(decideWith, "POST", `/approvals/${appr.id}/${decision}`, { payload_hash: appr.payload_hash }, ws);
  if (d.status !== 200) throw new Error("decide " + JSON.stringify(d));
  for (let i = 0; i < 60; i++) { await sleep(1000); const s = await api(who, "GET", `/plan-runs/${run}`, null, ws); if (["succeeded", "failed", "partial", "cancelled"].includes(s.body.status)) return { plan: id, run, status: s.body.status }; }
}
await api("owner", "POST", "/starter-workforce", {}, ws);
const done = await journey("owner", "admin", "approve");
const rejected = await journey("owner", "admin", "reject");
const waiting = await journey("owner", "admin", null);
fs.writeFileSync(path.join(OUT, "content.json"), JSON.stringify({ done, rejected, waiting }));
console.log({ done, rejected, waiting });

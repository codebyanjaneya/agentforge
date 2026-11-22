// Seeds owner/admin/member/starter/stranger through the real API into a shared workspace. Idempotent per fresh stack. Prints ids only.
import fs from "node:fs";
import path from "node:path";
import { api, PEOPLE, OUT } from "./lib.mjs";
for (const who of Object.keys(PEOPLE)) { const r = await api(who, "GET", "/workspaces"); if (r.status !== 200) throw new Error(`${who}: ${r.status}`); }
const ws = await api("owner", "POST", "/workspaces", { name: "Launch Crew" });
const id = ws.body.id; if (!id) throw new Error(JSON.stringify(ws));
for (const [who, role] of [["admin", "admin"], ["member", "member"], ["starter", "member"]]) {
  const r = await api("owner", "POST", `/workspaces/${id}/members`, { email: PEOPLE[who].email, role }, id);
  if (r.status !== 201) throw new Error(`${who}: ${r.status} ${JSON.stringify(r.body)}`);
}
const other = await api("stranger", "POST", "/workspaces", { name: "Other Co" });
fs.writeFileSync(path.join(OUT, "seed.json"), JSON.stringify({ workspace: id, other: other.body.id }));
console.log("seeded", { workspace: id, other: other.body.id });

# Staging verification plan: D074 (alerting, on-call, edge) and the staging-only drills

**Status: NOT RUN. Staging is not authorised and has not been accessed.** This plan adds precision to `docs/recovery-and-staging.md` section 2 and `docs/operations.md` sections 17, 18 and 21. It invents no endpoint, hostname, credential or receiver: every `<...>` below is something only the owner can supply. Nothing is "verified" until a row of the evidence table at the end is filled in with a real measurement from staging.

## A. What the owner must provide (exact list)

| # | Item | Why it is needed | Form |
|---|---|---|---|
| 1 | Written authorisation to connect to ONE staging environment, separate from production, holding no production credentials, data or DNS | nothing below can start without it | a sentence naming the environment |
| 2 | Staging access method: hosts or cluster, who may deploy, how I reach the shell and the logs | to deploy, run drills, read logs | access description (never a pasted secret) |
| 3 | A staging secret store entry built from `ops/staging.env.example` (all placeholders replaced) | `python ../ops/check_config.py --profile staging` must print `CONFIG GATE PASSED` | secret-store reference only |
| 4 | PostgreSQL 16 with TLS, WAL archiving permission, a backup/restore role and an archive target; a production-shaped (anonymised) backup to restore | migration, backup, PITR, RTO/RPO | connection reference, data size |
| 5 | Redis, preferably Sentinel (3 sentinels) and permission to stop/restart/fail over it | Redis and failover drills | topology description |
| 6 | At least 2 API and 2 worker processes or hosts, plus the Next.js app; permission to kill one worker | worker-takeover and duplicate-dispatch drills | host list |
| 7 | Real staging hostnames (`api.<domain>`, `app.<domain>`), certificates, and the nginx host | edge checks against real names and TLS | names, cert source |
| 8 | **A NON-PRODUCTION alert receiver** (the owner's choice: Slack test channel, PagerDuty test service, Alertmanager, or a webhook receiver they control) and its `ALERT_WEBHOOK_URL` | D074 delivery evidence | URL in the secret store; receiver type |
| 9 | **On-call route**: who is paged, by what channel (phone, SMS, app), the escalation if unacknowledged, and the name of a person who will confirm receipt at the time of the test | D074 "on-call delivery confirmation" cannot be inferred from a webhook 200 | named person + channel + test window |
| 10 | An external monitor of the owner's choice (uptime check on `/health`, `/ready` through the permitted path, and a Prometheus/Alertmanager scraping `/metrics` with `ops/alerts/agentforge-alerts.yml`) | the in-process evaluator cannot report its own death | monitor type and who owns it |
| 11 | `METRICS_TOKEN` handling (who may scrape) | readiness/metrics checks | secret-store reference |
| 12 | A test window and a rollback owner | failure drills interrupt service | date, name |

## B. Execution order and pass criteria

Each step records: start time, end time, command, exit code, observed result, and the file where the raw output is kept. A step that was skipped is recorded as skipped, never as passed.

1. **Configuration gate.** `cd backend && python ../ops/check_config.py --profile staging` against the real environment. PASS: `CONFIG GATE PASSED` and no warning about demo mode, a non-https origin, a missing `ALLOWED_HOSTS`, or a short `METRICS_TOKEN`.
2. **Readiness versus liveness.** For every API instance: `GET /health` is 200 while the database is up; `GET /ready` is 200 (private ranges only at the edge; from the public internet it must not answer). Then stop the staging database or block the DB port for ONE instance: `/ready` must return 503 `{"status":"unavailable"}` with no cause in the body, `/health` must stay 200, and the instance must leave the load balancer. Restore: `/ready` returns 200. PASS needs the timings and the load-balancer behaviour.
3. **Schema.** `python -m alembic upgrade head` on the restored staging copy; `verify_schema_version`; head is `0018_agent_slug_per_user`; a second upgrade is a no-op.
4. **Edge (real nginx, real names).** First `bash ops/edge_drill.sh` against the staging nginx config in a scratch environment (expect 13/13), then with `curl` against the real hostnames. PASS criteria: HTTP redirects to HTTPS; TLS 1.1 refused and 1.2/1.3 accepted; `/metrics`, `/docs`, `/redoc`, `/openapi.json` answer 404 from outside; `/ready` refused from outside the private ranges; a request with a wrong `Host:` header gets 400 (`ALLOWED_HOSTS`); a 2 MiB body gets 413 and a 5 KB body passes; `X-Forwarded-For` is overwritten (send a forged one and check the application log); token issuance beyond the per-IP limit gets 429 with `Retry-After`; an HTTP/1.1 WebSocket upgrade to `/ws` reaches the API, an idle socket survives the documented idle window, and a 64 KiB+ frame is refused; `uvicorn` runs with `--proxy-headers --forwarded-allow-ips <proxy>`.
5. **Alert delivery (D074 core).** Set `ALERT_WEBHOOK_URL` to the owner's NON-PRODUCTION receiver. Create a real firing condition in staging only (an unresolved dispatch: pause a send mid-flight and kill one API process, which yields an `unknown` outcome, or stop all workers while a run waits). PASS: exactly ONE firing message arrives at the receiver after the rule's `for` period, with the rule name, severity and a readable payload; the `ALERT_WEBHOOK_URL` value appears in no log line.
6. **Deduplication and recovery.** With two API processes running, the firing message arrives once (Redis claim). Resolve the condition (reconcile the dispatch): exactly ONE resolved message. Leave a condition firing for the repeat interval (default 4 h, or shorten it for the test with the owner's agreement): one reminder, not many. Stop Redis: delivery still happens (the fallback is "deliver anyway"), and the possible duplicate is recorded. Break the receiver (return 500, then time out): the notification stays pending, retries 3 times at 5 s, `agentforge_alert_delivery_failures_total` increments, and it is delivered once the receiver recovers.
7. **On-call confirmation.** The named person is paged through the owner's route and confirms the time they were reached and what they saw; record the end-to-end latency from the condition to the human. A webhook 200 is NOT on-call evidence.
8. **External monitoring.** Stop every API process for longer than the monitor's threshold: the EXTERNAL monitor must alert (the in-process evaluator cannot). Record time to alert. Load `ops/alerts/agentforge-alerts.yml` into the owner's Prometheus/Alertmanager and show the fleet-wide rules evaluating (a deliberate `up == 0` and a scrape with `METRICS_TOKEN`).
9. **Backup and PITR (staging only).** `ops/backup_job.py`: a verified run and the two metrics; the failed-restore variants (corrupt, truncated, mismatched) each fire the restore-verification alert. PITR: restore to a chosen instant into a NEW instance, time it, record the data size and compare row counts either side of the instant. Record RTO and RPO as measurements, with the data size next to them.
10. **Failure drills, one at a time** (`docs/recovery-and-staging.md` step 10): kill a worker mid-run (takeover time, duplicate sends must be 0), stop and restart Redis, Sentinel failover, stop the primary PostgreSQL and promote or restore (end-to-end RTO/RPO).
11. **Rollback rehearsal.** Deploy the previous release; schema stays forward-only; record the time.

## C. Evidence table (fill in; an empty row means NOT verified)

| Step | Status (PASS/FAIL/BLOCKED/NOT RUN) | Date and tester | Measurement | Evidence file |
|---|---|---|---|---|
| 1 Config gate | NOT RUN | | | |
| 2 Readiness vs liveness | NOT RUN | | | |
| 3 Schema | NOT RUN | | | |
| 4 Edge | NOT RUN | | | |
| 5 Alert delivery | NOT RUN | | | |
| 6 Dedup and recovery | NOT RUN | | | |
| 7 On-call confirmation | NOT RUN | | | |
| 8 External monitoring | NOT RUN | | | |
| 9 Backup and PITR | NOT RUN | | | |
| 10 Failure drills | NOT RUN | | | |
| 11 Rollback | NOT RUN | | | |

D073 and D074 stay "Directed, owner confirmation pending" until the owner has seen rows 4 to 8 (D074) and the Resend sandbox evidence (D073) and confirms them.

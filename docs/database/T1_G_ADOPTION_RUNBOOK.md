# T1-G — Database Adoption Runbook (Alembic baseline `0001_baseline`)

| | |
|---|---|
| Status | **DRAFT for owner review. Not executed.** No environment has been adopted. |
| Written | 2026-11-13 |
| Source of truth | `docs/database/T1_ALEMBIC_BASELINE_PLAN.md` §7, §8, §10, §12, §14; `backend/migrations/preflight.py`; `backend/migrations/env.py`; `backend/migrations/versions/0001_baseline.py` |
| Tag key | **[V-src]** read from this repository · **[V-test]** exercised by a repository test on SQLite/mocks · **[NV]** not verified anywhere · **UNKNOWN** an environment fact nobody has supplied |

> **T1-H update (2026-11-14).** The application code now contains a read-only startup schema-version guard (`backend/migrations/schema_guard.py`) and **no longer contains `init_db()` / `create_all`** [V-src]. This changes two things for adopters: (1) an **unadopted** database (no recorded `0001_baseline`) makes the *new* code **refuse to start** in every `APP_ENV` (fail closed), so adoption (this runbook) must be completed **before** the T1-H code is deployed to an environment; (2) gate G3 is still required whenever **any instance could still run pre-T1-H code** (see §1.3). The guard checks the recorded revision only; it does not prove schema parity and it never migrates or stamps. Nothing in this document has been executed against any environment.

> **M1-A.1 update (2026-11-14).** Since migration `0002_identity` the current head (`0002_identity`) is no longer the baseline. For an **unversioned** legacy database the pre-flight therefore compares the schema with the **frozen `0001_baseline` schema**, never with the current application models, and every report states what it was compared with (`compared against: 0001_baseline (frozen baseline schema, NOT the current head …)`). **MATCH means "matches `0001_baseline`"**; it never means "matches the current head". The adoption sequence is unchanged: verify the baseline match, stamp `0001_baseline` (explicit operator action), upgrade to the head, verify with the startup guard. An unversioned database that already has the head schema (for example the identity tables) is `DIVERGENT` and must not be stamped as the baseline. This was verified on SQLite and on a disposable PostgreSQL 16.15 server; it has not been run against any real environment.

> **This document authorizes nothing.** It is a procedure to be followed only after the owner gives explicit, written, per-environment approval. Reading it, or a clean pre-flight result, is not permission to run `alembic upgrade`, `alembic stamp`, a restore, or any other write against any database.

All commands below are **templates**. Every `<PLACEHOLDER>` must be filled in by the operator for one specific environment. No value in this document is a real host, database name, user, password, path or URL.

---

## 1. Scope and safety gates

### 1.1 Scope
- Brings **one existing PostgreSQL database at a time** under Alembic: either by creating the baseline in an empty database, or by recording (`stamp`) the baseline in a database that already matches.
- **Adoption is environment-specific.** Development, staging (if it exists) and production are separate databases with separate histories, separate evidence and separate approvals. Nothing learned in one environment is assumed for another. Complete §2–§9 independently for each.
- Out of scope: any schema change, corrective revision, startup change (T1-H), identity/JWT (T2), Docker/CI/deployment automation.

### 1.2 Gates — every gate must be satisfied and recorded before any write
| # | Gate | Owner-dependent? |
|---|---|---|
| G1 | **Written approval** from the owner naming the environment and the permitted action (the EMPTY sequence E1–E3, *or* the MATCH sequence M1–M5 including its `stamp 0001_baseline` and follow-on `upgrade head`; see §6.2). Approval for one environment does not extend to another. | Yes |
| G2 | **Approved maintenance window** with a named operator, start/end time, and a communicated freeze on **schema-changing deployments** to that environment. | Yes |
| G3 | **No application instance can run `init_db()` / `create_all` against the target during the window.** See §1.3. | Yes (deployment topology UNKNOWN) |
| G4 | **Verified backup** (§3): dump taken, restored into an isolated scratch database, and checkpoints passed. No backup, no write. | Yes |
| G5 | **Read-only evidence collected** (§4) and **pre-flight run** (§5) on the target and on the restored scratch copy, with the reports attached. | No |
| G6 | The action matches the classification in §6. Any other combination is a STOP. | No |

### 1.3 Preventing `init_db()` / `create_all` (gate G3)
- **Code versions.** *Before T1-H* (any instance still running the earlier code): `backend/main.py` calls `init_db()` unconditionally in the FastAPI `lifespan`, and `init_db()` runs `Base.metadata.create_all(engine)`, with **no setting, flag or environment variable** that disables it. *After T1-H* (current repository): that function and call are removed; `lifespan` runs the read-only schema guard instead, which never creates tables. G3 is therefore about **old code that may still be running or restarting** against the target; if the operator can demonstrate that **every** instance runs T1-H code, `create_all` cannot pre-empt the baseline, but the instances will refuse to start until the database is adopted, so they still must not be started against an unadopted target expecting them to work.
- **Behaviour of the pre-T1-H code [V-src, earlier revision]:** `create_all` only creates *missing* tables; it never alters existing ones and does not touch `alembic_version`.
- **Why it matters:** if any application instance starts against an **empty** target before the baseline is applied, `create_all` creates the two tables with no `alembic_version`, and a later `alembic upgrade head` then fails because the tables already exist. If it starts against a **divergent** database it may create only the missing tables and make the divergence harder to read.
- **Therefore, for any instance that may run pre-T1-H code, the only available control is operational:** every application instance (and anything else that imports the app against the target — workers, schedulers, one-off scripts, the Telegram bot process, if separate) must be **stopped, or demonstrably unable to reach the target database, from before the pre-flight until after the post-adoption checks**, and auto-restart mechanisms must be disabled for the window. How to do that depends on how the app is deployed, which is **UNKNOWN**; the operator records the method used in §2.
- If the operator cannot demonstrate G3, **STOP**. Do not proceed on the argument that `create_all` is "usually harmless".

---

## 2. Pre-adoption inventory (one copy per environment)

Fill this in before anything else. **Write `UNKNOWN` for anything not positively known; do not guess, and do not copy values from another environment.** Never record passwords, tokens, key material or full connection URLs — record *where a secret lives*, not the secret.

| Field | Value |
|---|---|
| Environment name | `<development | staging | production | other>` — UNKNOWN whether staging exists |
| Environment exists? | `<yes | no | UNKNOWN>` |
| `APP_ENV` value used by the app here | `<development | test | production>` — these are the only values `config.py` accepts [V-src]; unset means `production` |
| Database identity | `<DB_ALIAS>` (a human label, not a host/URL) · server version `<UNKNOWN until §4 query 1>` |
| Other applications sharing this database | `<UNKNOWN>` |
| Application version / git revision deployed | `<GIT_SHA or UNKNOWN>` |
| How the app is deployed and stopped | `<UNKNOWN>` |
| Operator | `<NAME or ROLE>` |
| Approver (owner) and approval reference | `<NAME or ROLE, TICKET/MESSAGE REF>` |
| Access method for the operator (e.g. direct, bastion, VPN) | `<UNKNOWN>` |
| Where the secret for the connection is held | `<SECRET STORE / FILE REFERENCE — not the secret>` |
| Backup location (path or storage reference) | `<BACKUP_LOCATION>` |
| Scratch (restore-test) server | `<SCRATCH_HOST_ALIAS>` — must not be the target server's production database |
| Maintenance window | `<START–END, TZ>` |
| Method used to guarantee G3 | `<DESCRIBE>` |
| Backup/restore capability, disk space, retention, restore time | `<UNKNOWN until tested>` [NV, plan R13] |
| Was the sample password in the README / git history ever used here? | `<UNKNOWN>` (plan R10) — owner to answer; if it was, rotate before relying on this database |

---

## 3. Backup and restore verification (gate G4)

Run **only inside the approved window** and only for the environment being adopted. Use libpq environment variables and a password file / secret store so that **no password and no URL appears on a command line, in shell history or in a report.** The command lines below are templates; nothing has been run.

```sh
# Target connection (placeholders). Password comes from a pgpass file or secret store, never inline.
export PGHOST=<TARGET_HOST> PGPORT=<TARGET_PORT> PGUSER=<TARGET_USER> PGDATABASE=<TARGET_DB>
export PGPASSFILE=<PATH_TO_PGPASS_FILE>        # or the operator's approved secret mechanism
```

### 3.1 Take the backup (read-only on the target)
```sh
pg_dump -Fc --no-owner -f <BACKUP_FILE>
sha256sum <BACKUP_FILE>                         # record the digest; PowerShell: Get-FileHash -Algorithm SHA256 <BACKUP_FILE>
pg_restore --list <BACKUP_FILE> > <TOC_FILE>    # table of contents; proves the archive is readable
```

### 3.2 Restore into an isolated scratch database
The scratch database must be on a **scratch server/instance (or at minimum a differently-named database that no application uses)**, never the target database.
```sh
# Scratch connection: DIFFERENT placeholders from the target.
export PGHOST=<SCRATCH_HOST> PGPORT=<SCRATCH_PORT> PGUSER=<SCRATCH_USER> PGDATABASE=postgres
createdb <SCRATCH_DB>
pg_restore --no-owner --exit-on-error --dbname=<SCRATCH_DB> <BACKUP_FILE>
```
- **Never** pass `--clean`, `--create`, or `--dbname=<TARGET_DB>` to `pg_restore` in this procedure. Restoring over the target is a recovery action (§8), not a verification step.
- Before every command that writes, re-read `PGHOST`/`PGDATABASE` and confirm they name the **scratch** side.

### 3.3 Verification checkpoints (all must pass; keep the evidence)
| # | Checkpoint | Evidence to retain |
|---|---|---|
| B1 | Dump command exited 0 and the file is non-empty | command output (no secrets), file size |
| B2 | SHA-256 of the dump recorded | digest |
| B3 | `pg_restore --list` succeeds | `<TOC_FILE>` |
| B4 | Restore into the scratch DB exited 0 with `--exit-on-error` | command output |
| B5 | Row counts match between target and scratch for **every** table (at minimum `agent_memories`, `telegram_links`, and any unmanaged table found in §4): `SELECT count(*) FROM <TABLE>;` run on each side | both counts side by side |
| B6 | Catalog evidence (§4 queries 2–5) run on the scratch DB is identical to the target's | both outputs |
| B7 | The pre-flight (§5) run against the **scratch** copy returns the same state as against the target | both reports |

If any checkpoint fails: **STOP.** A backup that has not been restored and compared is not a backup for this procedure.

---

## 4. Read-only evidence collection (target, then scratch)

Run on the **target** and again on the **restored scratch copy**. Make the session read-only at the server so a mistyped statement cannot write:
```sh
export PGOPTIONS='-c default_transaction_read_only=on'   # standard PostgreSQL setting; not exercised by this repository [NV]
psql -X -v ON_ERROR_STOP=1 -f <EVIDENCE_SQL_FILE> > <EVIDENCE_OUTPUT_FILE>
```
`<EVIDENCE_SQL_FILE>` contains the queries below, **copied unchanged from `T1_ALEMBIC_BASELINE_PLAN.md` §12** (their intent is to establish the *actual deployed state*, which the plan states is UNKNOWN). Reports must never contain credentials or a full connection URL; redact the output if the server echoes any.

```sql
SELECT version();
SELECT table_schema, table_name FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog','information_schema') ORDER BY 1,2;
SELECT table_name, column_name, data_type, character_maximum_length, is_nullable, column_default
  FROM information_schema.columns WHERE table_schema = 'public' ORDER BY table_name, ordinal_position;
SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' ORDER BY 1,2;
SELECT conrelid::regclass AS tbl, conname, contype, pg_get_constraintdef(oid) FROM pg_constraint WHERE connamespace = 'public'::regnamespace ORDER BY 1,2;
SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY 1;   -- approximate row counts
SELECT has_schema_privilege(current_user, 'public', 'CREATE') AS can_create;
```

| Query | Purpose | Required? |
|---|---|---|
| 1 `version()` | PostgreSQL version | **Required** |
| 2 tables (all non-system schemas) | shows whether anything exists, in which schema, and any unmanaged tables | **Required** |
| 3 columns (`public`) | compare with `memory/models.py` / `0001_baseline.py` (types, nullability, `nextval(...)` defaults on `agent_memories.id` and `telegram_links.chat_id`) | **Required** |
| 4 indexes (`public`) | expect exactly `ix_agent_memories_agent_name` and `ix_agent_memories_user_email` (non-unique) | **Required** |
| 5 constraints (`public`) | expect only the two primary keys (`agent_memories_pkey`, `telegram_links_pkey`); no foreign, unique or check constraints | **Required** |
| 6 `pg_stat_user_tables` row estimates | approximate sizes (they are estimates; B5 uses exact `count(*)`) | Optional |
| 7 `has_schema_privilege(... 'CREATE')` | the connecting role must be able to create objects in `public` for `upgrade` (creates tables) and `stamp` (creates `alembic_version`) | **Required before any write** |

Also record (from the plan §12): whether other applications share the database, backup/restore tooling, and who runs deployments — each as a value or `UNKNOWN`.

**Manual comparison.** The pre-flight has never been run against PostgreSQL [NV]. Its PostgreSQL-specific checks and `compare_metadata` behaviour on PostgreSQL reflection are unverified, so a human must compare queries 2–5 with the expected schema independently of the pre-flight result, and record agreement or disagreement.

---

## 5. Pre-flight classification (read-only)

### 5.1 Invocation
Run from the `backend/` directory, inside the backend virtual environment (which provides the pinned `alembic==1.20.1` [V-src: `backend/requirements.txt`]). The pre-flight reads only `APP_ENV` and `DATABASE_URL`; it needs no other application setting.

```sh
cd backend
export APP_ENV=<development|production>        # the value for THIS environment; never "test" (sqlite is accepted only under test)
export DATABASE_URL=<SET_FROM_SECRET_STORE>    # must be a postgresql:// URL; do not type it inline or echo it
python -m migrations.preflight
echo "exit code: $?"
```
It performs inspector and `SELECT` reads only and **never prints the URL or raw driver text** [V-test on SQLite]. Run it twice: against the **target** and against the **restored scratch copy** (checkpoint B7), changing only `DATABASE_URL`. Before each run, confirm which database `DATABASE_URL` points to by its **alias**, not by printing the URL.

### 5.2 States and exit codes (approved mapping [V-src, V-test])
| Exit | State | Meaning |
|---|---|---|
| 0 | `EMPTY` | inspection succeeded; the schema (PostgreSQL: the `public` schema) has **no tables at all** and no `alembic_version` |
| 10 | `MATCH` | no `alembic_version`, and the database matches the **frozen `0001_baseline` schema** exactly (tables, columns, types, nullability, primary keys, indexes; nothing unmanaged). It says nothing about the current head: the report prints `compared against: 0001_baseline (frozen baseline schema, NOT the current head …)` |
| 11 | `ALREADY_VERSIONED_CURRENT` or `ALREADY_VERSIONED_BEHIND` | `alembic_version` holds exactly one known revision (the report says whether it is a head or known-but-not-a-head) |
| 20 | `DIVERGENT` | any difference (missing/extra table, column or index; type, nullability or primary-key difference; extra check constraint; PostgreSQL sequence/default/primary-key-name finding) **relative to the frozen `0001_baseline` schema** (this includes an unversioned database that already has the head schema, e.g. the identity tables, which also gets a dedicated finding saying it must not be stamped as the baseline), **or the PostgreSQL current schema is not `public`** |
| 21 | `ALREADY_VERSIONED_UNKNOWN` | `alembic_version` exists but is malformed (no `version_num`, zero or several rows, NULL/empty) or names a revision not in `backend/migrations/versions/` |
| 30 | `UNINSPECTABLE` | connection or inspection failed (the report shows only an exception class name) |
| 2 | usage/configuration error | `APP_ENV` / `DATABASE_URL` missing or invalid; **not** a classification of the database |

Always capture the full report text, not just the exit code. **Do not script "exit code 0 ⇒ proceed"**: 0 means *EMPTY*, not "OK".

Example report for an unversioned database that matches the baseline (the head named is the repository's current head at the time):
```
preflight state: MATCH
exit code: 10
compared against: 0001_baseline (frozen baseline schema, NOT the current head 0002_identity)
note: matches 0001_baseline only and does NOT match the current head; this is a classification, not authorization: stamping the baseline and upgrading to the head are separate explicit operator actions
```
Check the `compared against:` line in every report you attach: for an unversioned database it must name `0001_baseline`; for a versioned database it names the recorded revision and says the schema was not compared; for an empty database it says the database is empty.

### 5.3 What the pre-flight is not
- **What an unversioned database is compared with [V-test].** The frozen `0001_baseline` schema (`backend/migrations/baseline_schema.py`, verified against the T1-A plan DDL and against what the baseline migration really creates), not the application models. Consequently a baseline-only legacy database is `MATCH` even though the head has more tables, a database that already has the head schema but no `alembic_version` is `DIVERGENT`, and a `MATCH` never authorizes `stamp head` or skipping the baseline stamp.
- **A classification is not authorization.** No exit code permits any migration or stamp; permission comes only from gate G1 and the matrix in §6.
- **PostgreSQL `public` schema requirement.** The pre-flight checks `SELECT current_schema()` first; anything other than `public` (including NULL) is `DIVERGENT`, whatever else is present, including for versioned databases [V-test with mocks]. This procedure assumes a single `public` schema [plan §15, assumption 2].
- **Versioned databases are classified by revision, not by schema parity.** For exit 11 and 21 the schema is **not** compared at all. A known revision in `alembic_version` is a record of what someone stamped or applied, not proof that the tables match.
- **PostgreSQL runtime behaviour is unverified [NV]:** the pre-flight and its PostgreSQL-specific checks (current schema, owned sequences and `nextval` defaults, `<table>_pkey` names, the `postgresql_readonly` transaction setting, reflection of `TIMESTAMP`/`SERIAL`) were tested only with SQLite and mocks. A `DIVERGENT` on a database that looks right may be a false positive; a `MATCH` is not a substitute for the manual comparison in §4. When the two disagree, **STOP** and treat the discrepancy as a finding.

---

## 6. Decision and action matrix

The table gives the **only** permitted action for each result. Everything else is a STOP. **Neither this runbook nor any pre-flight exit code authorizes any row:** each action additionally requires gate G1 (written, per-environment owner approval) and all of G1–G6. Throughout every sequence below, **all application instances stay stopped or unreachable** (§1.3) until the sequence and its §7 verification are complete; there is no startup switch yet.

| Pre-flight result | Action | Prerequisites (all of G1–G6) |
|---|---|---|
| **EMPTY (0)** | Fresh-database path (plan §8.3), sequence **E1–E3** in §6.2: `alembic upgrade head` (creates both tables, both indexes, and `alembic_version` = `0001_baseline`), verify the revision, **then** start the application. | G1 approval naming `upgrade head`; §4 evidence **independently** confirms the intended database is empty (so the connection is not pointed at the wrong database); backup taken even though empty if the owner requires it; **no application instance started before this succeeds** (§1.3). |
| **MATCH (10)** | Adoption sequence **M1–M5** in §6.2: verified backup and owner approval → `alembic stamp 0001_baseline` (**explicit revision id, never `head`**; writes only the `alembic_version` table) → `alembic current` must show `0001_baseline` → `alembic upgrade head` → post-action verification (§7). | G1 approval naming `stamp`; **verified backup (§3, B1–B7)**; the §4 manual comparison agrees with MATCH (a match with `0001_baseline`, per the report's `compared against:` line); role has CREATE on `public` (§4 query 7). |
| **DIVERGENT (20)** | **STOP normal adoption.** No upgrade, no stamp, no `create_all`, no manual repair on the strength of this result alone, and **never run `0001_baseline` (`alembic upgrade head`) against a divergent database.** Follow the corrective path **D1–D7** in §6.2, which ends by returning to the MATCH sequence. Never drop unmanaged tables or columns without owner sign-off and a backup (plan §10). | — (corrective work is a new, separately approved task) |
| **UNINSPECTABLE (30)** | **STOP.** Resolve connectivity, credentials, privileges or the cause of the failure (the report gives only a class name; investigate with the database administrator using approved channels, not by widening the report). Re-run; do not assume EMPTY or MATCH. | — |
| **ALREADY_VERSIONED known (11)** | **No stamp, no upgrade.** Record the revision and whether it is a head. Treat it as "someone already adopted or upgraded this database". Any further `alembic upgrade` needs a **separate review and approval**, and an independent comparison of §4 evidence if schema parity matters (it is not proven by this state). | Separate owner review |
| **ALREADY_VERSIONED unknown/malformed (21)** | **STOP.** Do not run `upgrade`, `stamp` or edit `alembic_version`. Capture the `alembic_version` contents with a read-only `SELECT version_num FROM alembic_version;` for the owner; the owner decides the next step. | — |
| **Usage/config error (2)** | Fix `APP_ENV`/`DATABASE_URL` (variable *names* are reported, never values) and re-run. Not a database result. | — |

### 6.1 Action command templates (run only when the matrix and gates allow; **not executed by this task**)
Run from `backend/`, with the same `APP_ENV`/`DATABASE_URL` mechanism as §5.1. `env.py` takes the URL from `config.get_database_url()`; `alembic.ini` contains no URL [V-src]. The online path uses `NullPool` and runs inside one transaction; on PostgreSQL it first takes a transaction-scoped advisory lock [V-src; the PostgreSQL branch has never been executed [NV]].

```sh
# EMPTY only (sequence E1-E3):
alembic upgrade head
alembic current                 # must show 0001_baseline

# MATCH only (sequence M1-M5):
alembic stamp 0001_baseline
alembic current                 # must show 0001_baseline BEFORE continuing
alembic upgrade head            # no-op while 0001_baseline is the only head; see M4
```
- Do **not** use `stamp head`, `--purge`, `alembic downgrade`, `alembic revision --autogenerate`, or any `--sql` output piped to a database.
- `alembic stamp` and `alembic current` have never been run through this project's online runner, even on SQLite [NV]; only `upgrade head` was (SQLite, T1-E-R1). Expect them to behave normally, and treat any unexpected output as a STOP.
- If a command fails, **do not retry blindly.** Capture the error text (redacting secrets), re-run the read-only pre-flight, and STOP for review.

### 6.2 Ordered sequences (each step needs the previous one to succeed; any surprise is a STOP)

**E — EMPTY (fresh-database path, plan §8.3).** Prerequisite: gates G1–G6, application instances stopped/unreachable.
- **E1.** `alembic upgrade head` (§6.1).
- **E2.** Verify the revision: `alembic current` shows `0001_baseline`, and the §7 checks pass (new objects are exactly `agent_memories`, `telegram_links`, `alembic_version` and the two indexes).
- **E3.** **Only now** start the application (T1-H code: the startup guard then passes because `alembic_version` = `0001_baseline`; pre-T1-H code: its `init_db()` is a no-op), and run the §7 smoke checks. Starting it earlier would let `create_all` pre-empt the baseline (§1.3).

**M — MATCH (adoption path, plan §8.2 step 5).**
- **M1.** Verified backup (§3, B1–B7) **and** owner approval naming `stamp 0001_baseline` and the follow-on `upgrade head` (G1, G4). The §4 manual comparison agrees with MATCH, and the report's `compared against:` line names `0001_baseline` (MATCH is a baseline match, not a head match).
- **M2.** `alembic stamp 0001_baseline` (§6.1; never `head`).
- **M3.** `alembic current` must show `0001_baseline`. If it does not, **STOP**.
- **M4.** Confirm the script directory has the single head `0001_baseline` (the T1-E tests assert this; e.g. `alembic heads`), then `alembic upgrade head`. With only the baseline revision this is a no-op. If any later revision exists, **STOP** and obtain separate review and approval of that revision; this approval covers the baseline only.
- **M5.** Post-action verification (§7): `alembic current`, pre-flight re-run (expect exit 11), `SELECT version_num FROM alembic_version;`, §4 before/after comparison (only `alembic_version` is new), then start the application and run the smoke checks.

**D — DIVERGENT (corrective path; adoption is not allowed until the database is MATCH).**
- **D1.** **STOP normal adoption.** Record the full pre-flight report and the §4 evidence; do not run `upgrade`, `stamp` or `create_all`.
- **D2.** Owner decides between a **separate reviewed corrective revision** (for example adding `agent_memories.user_email` and its index for the historical case) and an **approved manual fix**. Either is a new task with its own review; this runbook does not authorize it.
- **D3.** **Test the chosen fix on an isolated restored backup** (the §3 scratch database), never on the target first. Re-run the pre-flight on the scratch database until it reports MATCH there.
- **D4.** **Apply to the target only after explicit owner approval** of the tested fix, inside an approved window with a fresh verified backup (G1–G4 again).
- **D5.** Re-run the pre-flight on the target. If it is not MATCH, **return to D1**.
- **D6.** Only when the pre-flight reports MATCH (against `0001_baseline`) **and** the §4 manual comparison agrees, follow sequence **M** from M1 (new approval and backup as M1 requires).
- **D7.** `0001_baseline` is never executed against a divergent database, at any point in this path.

---

## 7. Post-adoption verification (templates; run after the action, same session)

```sh
cd backend
alembic current                 # expect: 0001_baseline (head)   [exact output format NV]
python -m migrations.preflight  # expect exit 11, state ALREADY_VERSIONED_CURRENT
```
```sql
-- read-only, same read-only session settings as §4
SELECT version_num FROM alembic_version;     -- expect exactly one row: 0001_baseline
```
Then, in order:
1. **Re-run §4 queries 2–5** and compare with the pre-action output. After `stamp`, the *only* difference must be the new `alembic_version` table. After `upgrade head` on an EMPTY database, the new objects must be exactly: `agent_memories`, `telegram_links`, `alembic_version`, and the two indexes. Any other difference is a STOP.
2. **Application smoke checks** (only after step 1 and the checks above pass; this is the first point at which the application may be started, and the startup guard then passes, or, for pre-T1-H code, `init_db()` is a no-op because the tables exist). Use a **designated test identity and a test chat id agreed with the owner**, never real user data:
   - **History read:** read the history for the test user email through the application's history read path (`LongTermMemory.get_history` in `backend/memory/long_term.py`, or the equivalent route the owner names) and confirm no database error.
   - **Telegram-link read/write:** `get_link`, `set_email`, `set_active_agent` in the same module for the test chat id: write, read back, confirm the values, and confirm `chat_id` auto-increment/primary key behave (no sequence or default error). Any cleanup of test rows requires the owner's approval.
   The exact routes and procedure for exercising these on a live deployment are **UNKNOWN**; the owner supplies them.
3. **Record** in the adoption record (§9): operator, date/time, environment, the revision recorded, and references to every evidence file.

Adoption is **complete only when the §9 checklist is fully attached and the owner signs off.** Until then the environment is *not adopted*, and T1-H code must not be deployed to it (it would refuse to start).

---

## 8. Rollback, recovery and stop conditions

### 8.1 Policy: forward-only
- `0001_baseline.downgrade()` **intentionally raises** `RuntimeError` and performs no operation [V-src, V-test]. **Never run `alembic downgrade` against any environment.** There is no supported way to "undo" the baseline by downgrading.
- Corrections are **new forward revisions**, separately reviewed and approved.
- **Recovery from a bad outcome is restore from the verified backup** (§3), performed under a separate owner-approved plan (target a new/empty database or a database the owner confirms may be overwritten; never improvise `--clean` against production). Restore capability and time are **UNKNOWN** until §3 has been done once.
- A mistaken `stamp` writes only `alembic_version`. The plan (§10) names a manual, reviewed correction of that one row as the fallback, but its exact behaviour is **[NV]** and it is **not authorized by this document**: STOP and take the proposed statement to the owner for review before anything is run.
- PostgreSQL DDL is transactional, so a failed `upgrade head` normally leaves no partial state; confirm that with a fresh pre-flight rather than assuming it [NV on this project's PostgreSQL].

### 8.2 Immediate STOP conditions (stop, preserve evidence, escalate to the owner)
1. Any gate G1–G6 not satisfied or not recorded.
2. Any application instance running or restarting against the target during the window (G3), or any doubt about it.
3. Uncertainty about which database a connection points at (alias unclear, wrong environment, a developer's database).
4. Backup, restore or any B1–B7 checkpoint fails or is skipped.
5. Pre-flight result is DIVERGENT, UNINSPECTABLE, or versioned-unknown (21), or disagrees with the §4 manual comparison.
6. The current schema is not `public`.
7. The connecting role lacks CREATE on `public` for a write step, or the action requires more privileges than the owner approved.
8. Any command output contains a credential, a connection URL, or unexpected host/user text — stop, redact, and treat the secret as exposed (rotate).
9. Any command other than the ones in §6.1 appears necessary, or any error whose cause is not understood.
10. Post-action verification (§7) differs from the expected result in any way.
11. The maintenance window expires before verification finishes.
12. Anyone proposes `downgrade`, `create_all`, `stamp head`, dropping unmanaged objects, or restoring over the target without a separate approved plan.

---

## 9. Evidence checklist (attach before an environment may be declared adopted)

One record per environment. Every item is an artifact reference; none may contain secrets or full URLs.

- [ ] Environment name, `APP_ENV`, database alias, operator, approver, approval reference (§2)
- [ ] Written owner approval naming the environment and the single permitted action (G1)
- [ ] Maintenance-window record and the schema-changing-deployment freeze notice (G2)
- [ ] Description of how `init_db()`/`create_all` was prevented for any pre-T1-H instance, and proof (G3); or proof that every instance runs T1-H code
- [ ] Backup: command output, file size, SHA-256, TOC listing (B1–B3)
- [ ] Scratch restore: output, row counts for every table on both sides (B4–B5)
- [ ] §4 catalog evidence from the target **and** the scratch copy, with the manual comparison note (B6)
- [ ] Pre-flight report text and exit code from the target and from the scratch copy (B7)
- [ ] The decision taken per §6 and its justification
- [ ] Exact action command run, with timestamp and operator, and its output (credentials redacted)
- [ ] `alembic current` output after the action
- [ ] Pre-flight re-run report (expect exit 11 / `ALREADY_VERSIONED_CURRENT`)
- [ ] `SELECT version_num FROM alembic_version;` output (expect `0001_baseline`)
- [ ] §4 queries 2–5 re-run and the before/after comparison
- [ ] Application smoke-check results for history read and Telegram-link read/write
- [ ] Owner sign-off, date, and the entry recorded in `flow.md` (and `decision.md` if the owner so decides)

---

## 10. Owner-dependent items still unknown (nothing here is assumed)

| # | Item | Status |
|---|---|---|
| 1 | Which environments exist (development, staging, production) and which are to be adopted | UNKNOWN |
| 2 | Whether each database is empty, populated, or hand-altered | UNKNOWN (plan §13; likely divergent for older databases because `agent_memories.user_email` and its index were added 2026-06-22) |
| 3 | How the application is deployed, stopped, and prevented from auto-restarting | UNKNOWN |
| 4 | Backup/restore tooling, disk space, retention, restore time; availability of a scratch PostgreSQL instance | UNKNOWN |
| 5 | Who is the named operator and approver; the maintenance-window policy and downtime tolerance | UNKNOWN |
| 6 | Whether the sample password from the README/git history was ever used in a real environment; credential rotation | UNKNOWN |
| 7 | How to exercise history and Telegram-link smoke checks on each live deployment, and which test identity to use | UNKNOWN |
| 8 | Behaviour of the pre-flight, `alembic stamp`, `alembic current` and the advisory-lock branch on a real PostgreSQL | [NV] — rehearse on the restored scratch copy first |
| 9 | Removal of `init_db()`/`create_all` and the startup schema-version check | T1-H implemented in the repository (not yet deployed anywhere); deployment ordering per the T1-H banner above |

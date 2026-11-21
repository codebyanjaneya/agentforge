# T1-A — Alembic Baseline: Inspection and Implementation Plan

> Status: **PLAN ONLY. Nothing in this document is implemented.** Date: 2026-11-12. Commit base: `c841cef` plus the (uncommitted) T0 work.
> No database was connected to, no schema or data was touched, nothing was installed, no manifest was edited.

**Evidence tags used on every important statement**

| Tag | Meaning |
|---|---|
| **[V-src]** | Verified by reading source code or git history |
| **[V-test]** | Verified by a local isolated test (no PostgreSQL, no network; SQLAlchemy DDL compilation and throw-away SQLite files) |
| **[NV]** | Not verified (could not be checked safely/read-only) |
| **[REC]** | Recommendation (a proposal, not a fact) |

---

## 1. Executive summary

- The schema is small: two tables (`agent_memories`, `telegram_links`), one declarative `Base`, all defined in `backend/memory/long_term.py`. **[V-src]** There are no foreign keys, no unique constraints, no server-side defaults and no naming convention beyond SQLAlchemy's default index naming. **[V-test]**
- Tables are currently created only by `Base.metadata.create_all` (via `init_db()` in the application lifespan since T0). **[V-src]** `create_all` never alters existing tables — shown locally. **[V-test]**
- **The deployed database state is unknown.** **[NV]** Git history shows `agent_memories` originally had no `user_email` column (commit `79d05a6`, 2026-06-19) and gained it plus an index on 2026-06-22 (`de0fe86`). **[V-src]** Because `create_all` does not add columns, a database created before that date and never altered by hand would **lack `user_email` and its index**, i.e. diverge from today's models. **[REC: treat as a real possibility.]**
- Therefore the baseline must never be adopted blindly. The plan separates four database states — **empty**, **exactly matches metadata**, **divergent/partial**, **cannot be inspected** — with different handling for each. `alembic stamp head` against an unclassified database is explicitly rejected. **[REC]**
- Recommended tool: **Alembic 1.20.1**, which a pip dry-run resolved against the installed SQLAlchemy 2.0.51 / Python 3.11.9 with no change to existing packages (adds only Mako and MarkupSafe). **[V-test: resolver dry-run, see §11]** Needs approval before installation.
- Key design points: migrations directory named `migrations/` (not `alembic/`, to avoid shadowing the package); a metadata-only `memory/models.py` so Alembic need not import the engine or LLM settings; the database URL read from the existing settings and never written into config files or logs; a baseline revision whose `downgrade()` refuses to drop tables; a read-only pre-flight classifier gating any `stamp`; `init_db()`/`create_all` removed from startup only after each environment is adopted. **[REC]**

---

## 2. Current database architecture

| Aspect | Finding | Tag |
|---|---|---|
| Single DB module | `backend/memory/long_term.py` holds the engine, `SessionLocal`, `Base`, both models, `init_db()`, `LongTermMemory`, and the Telegram link helpers (`get_link`, `set_email`, `set_active_agent`). | V-src |
| Engine | `create_engine(get_settings().database_url.get_secret_value(), pool_pre_ping=True, pool_recycle=1800)` at import time; lazy — no connection until first use. | V-src, V-test |
| Sessions | `sessionmaker(bind=engine, expire_on_commit=False)`; one `with SessionLocal() as session:` per call; synchronous psycopg2; called from async code (Telegram handlers wrap in `asyncio.to_thread`, agents do not). | V-src |
| Driver | `psycopg2-binary` 2.9.12 installed; URL scheme `postgresql://` (default psycopg2 dialect). T0 settings reject non-`postgresql` schemes (SQLite only with `APP_ENV=test`). | V-src |
| Schema creation | `init_db()` → `Base.metadata.create_all(engine)`, called in `main.py` `lifespan` via `asyncio.to_thread`. Before T0 it ran at import. | V-src |
| Other consumers of the DB module | the seven `agents/*.py`, `improvement/analyzer.py`, `telegram_bot.py`, `main.py` (all import from `memory.long_term`). Nobody else defines models or a `Base`. | V-src |
| Migration tooling | **None.** No `alembic*`, `migrations/`, SQL files or migration mentions in the repo, README, `flow.md` or `decision.md` apart from "Alembic arrives in T1". `alembic` is not installed. | V-src, V-test |
| Dependency manifests | `requirements.txt` lists `sqlalchemy` and `psycopg2-binary` **unpinned**; `requirements-dev.txt` = `-r requirements.txt` + `pytest==9.2.0`. | V-src |
| Database docs | README only documents running PostgreSQL via Docker on port 5433 with sample credentials; `flow.md` §10 documents the two tables. | V-src |

Historical note: the first commit hard-coded a localhost DB URL **including a password that matches the sample password documented in the README**; it was replaced by an env var in `4697f44`. The password is therefore in git history. **[V-src]** (Value deliberately not reproduced.) Whether any real database ever used that password is **[NV]**; **[REC]** confirm production credentials were rotated.

---

## 3. Verified existing schema (from model metadata)

Source of truth for this section is the SQLAlchemy metadata imported from `backend/memory/long_term.py` and compiled for the PostgreSQL dialect **without connecting** (import with an in-process SQLite URL, socket connect disabled, DDL compiled with `postgresql.dialect()`). **[V-test]** This is *model metadata*, **not** the deployed database. **[NV for deployed state]**

```sql
CREATE TABLE agent_memories (
	id SERIAL NOT NULL,
	agent_name VARCHAR(100) NOT NULL,
	user_email VARCHAR(255),
	summary TEXT NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (id)
);
CREATE INDEX ix_agent_memories_agent_name ON agent_memories (agent_name);
CREATE INDEX ix_agent_memories_user_email ON agent_memories (user_email);

CREATE TABLE telegram_links (
	chat_id BIGSERIAL NOT NULL,
	email VARCHAR(255),
	active_agent VARCHAR(100) NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	PRIMARY KEY (chat_id)
);
```

| Property | Finding | Tag |
|---|---|---|
| Tables | `agent_memories`, `telegram_links`; default schema (`public`), no explicit schema | V-test |
| Foreign keys / unique / check constraints | **None** | V-test |
| Primary keys | `agent_memories.id` (Integer, autoincrement → `SERIAL`); `telegram_links.chat_id` (BigInteger → **`BIGSERIAL`** because SQLAlchemy treats a lone integer PK as autoincrement, although the application always supplies `chat_id`) | V-test |
| Indexes | `ix_agent_memories_agent_name`, `ix_agent_memories_user_email` (both non-unique); none on `created_at`, `telegram_links.email` | V-test |
| Nullability | nullable: `agent_memories.user_email`, `telegram_links.email`; everything else NOT NULL | V-test |
| Defaults | **No server defaults at all.** `created_at`/`updated_at` use Python-side `datetime.utcnow` (naive UTC, no timezone), `updated_at` also Python-side `onupdate`; `active_agent` default `"ceo"` is Python-side only. Raw SQL inserts that omit these NOT NULL columns would fail. | V-test |
| Timestamps | `TIMESTAMP WITHOUT TIME ZONE`, naive UTC | V-test |
| Naming conventions | Metadata `naming_convention == {'ix': 'ix_%(column_0_label)s'}` (SQLAlchemy default). PK constraints are unnamed in metadata, so PostgreSQL assigns `<table>_pkey`; serial sequences are PG-assigned `<table>_<col>_seq`. | V-test; PG names **[NV]** |
| Row-level data | Unknown volume/content | NV |

### 3.1 Schema history (why divergence is plausible) — [V-src]

| Commit | Date | Change to `long_term.py` |
|---|---|---|
| `79d05a6` | 2026-06-19 | `agent_memories(id, agent_name idx, summary, created_at)` — **no `user_email`** |
| `de0fe86` | 2026-06-22 | adds `user_email VARCHAR(255) NULL` with an index |
| `4697f44` / `8ac0626` | 2026-06-22/23 | engine from env; `pool_pre_ping`/`pool_recycle` (no schema change) |
| `b210b1e` | 2026-06-24 | adds table `telegram_links` |

`create_all` creates missing **tables** but never adds columns or indexes to existing ones. **[V-test]** A database first created on 2026-06-19–21 and never altered would therefore be missing `user_email` and its index, yet the code would run until a write/read touching that column. Whether production was recreated, altered by hand, or created after 2026-06-22 is **[NV]**.

---

## 4. Metadata discovery / import strategy

**Goal:** `target_metadata` in Alembic's `env.py` must contain every table, and importing it must not require an engine, a database or unrelated secrets.

Problem verified in source: `memory/long_term.py` builds the engine at import via `get_settings()`, and `get_settings()` requires `GROQ_API_KEY` as well as `DATABASE_URL`. **[V-src]** An Alembic CLI that imports `memory.long_term` would therefore need LLM credentials in its environment and would create an engine it does not use.

**[REC] Proposal (small, behavior-preserving):**
1. New `backend/memory/models.py` containing only `Base` and the two model classes (moved verbatim). `memory/long_term.py` does `from memory.models import Base, AgentMemory, TelegramLink` so all existing imports keep working; `init_db()`, `engine`, `SessionLocal` stay where they are.
2. `migrations/env.py` imports `memory.models` only and sets `target_metadata = Base.metadata`.
3. Completeness guard test: (a) `set(Base.metadata.tables) == {"agent_memories", "telegram_links"}` (updated whenever a table is added deliberately); (b) a static scan failing the suite if any module under `backend/` (excluding `migrations`, `tests`, `venv`) subclasses `Base` outside `memory/models.py`, so a model can't be created without being visible to Alembic; (c) `memory.long_term.Base is memory.models.Base`.
4. Alternative if the owner prefers not to move code: `env.py` imports `memory.long_term` with a fully populated environment (works, but couples migrations to the app's secrets). **Not recommended.**
5. **Naming convention [REC, needs owner decision before T2 adds foreign keys]:** set `Base.metadata = MetaData(naming_convention={...})` using conventions that **reproduce PostgreSQL's default names** so existing objects need no rename: `pk: "%(table_name)s_pkey"`, `uq: "%(table_name)s_%(column_0_name)s_key"`, `fk: "%(table_name)s_%(column_0_name)s_fkey"`, `ix: "ix_%(column_0_label)s"` (unchanged), `ck: "ck_%(table_name)s_%(constraint_name)s"`. Without a convention, future constraint names are PG-generated and unportable across `drop_constraint` migrations. Adding it does not rename anything in an existing database by itself **[NV: verify on a disposable PostgreSQL]**.

---

## 5. Alembic configuration proposal [REC]

Layout (all under `backend/`, the directory from which `uvicorn main:app` and pytest already run):
```
backend/alembic.ini            # no database URL, no credentials
backend/migrations/env.py
backend/migrations/script.py.mako
backend/migrations/versions/0001_baseline.py
```
`migrations/` (not `alembic/`) so the directory cannot shadow the installed `alembic` package when `backend/` is on `sys.path`.

`alembic.ini`: `script_location = migrations`, a timestamp-free sequential `file_template` (e.g. `%%(rev)s_%%(slug)s`), **no `sqlalchemy.url`**, logging for `alembic`/`sqlalchemy` at WARN (INFO would log SQL), `prepend_sys_path = .`.

`env.py` requirements:
- **URL source:** read it from the existing settings layer, never from the ini file. Because `get_settings()` also demands `GROQ_API_KEY`, add a small `config.get_database_url()` (validates only the DB variable, same `postgresql://` rules) for tooling — or reuse `load_settings` if the owner accepts the extra requirement. The URL is held as a `SecretStr`/local variable only; it is **not** passed through `config.set_main_option` (ConfigParser would also mangle `%` characters and could expose it in debug output).
- **Online mode:** `create_engine(url, poolclass=NullPool)`; `context.configure(connection=..., target_metadata=..., compare_type=True, compare_server_default=True, include_object=<filter>)`; run inside one transaction (PostgreSQL DDL is transactional) so a failing migration leaves no partial state.
- **Offline mode (`--sql`):** `context.configure(url=<placeholder dialect URL without credentials>, literal_binds=True, dialect_opts={"paramstyle": "named"})`. Offline mode only needs the **dialect**, so it can be driven by a credential-free `postgresql://` placeholder; the generated SQL contains no connection information. Used for review and for the DB-free parity tests.
- **`include_object` filter:** ignore `alembic_version` and **never** treat database tables absent from metadata as drops (autogenerate must not emit `DROP TABLE` for unmanaged/legacy tables).
- **Logging hygiene:** if `fileConfig` is used, pass `disable_existing_loggers=False`; never log the URL; wrap connection errors so only the exception class and a generic message are printed (driver errors can include host/user text — **[NV]** exact content for psycopg2).
- **Version table:** default `alembic_version`, default schema.
- **Concurrency:** migrations are run as a **separate one-shot deploy step**, never from the app lifespan, and `env.py` takes a PostgreSQL advisory lock for the duration so two simultaneous runs serialize. Whether Alembic itself provides locking is **[NV]**.

---

## 6. Baseline strategy [REC]

1. **Revision `0001_baseline`** reproduces exactly the schema in §3 with explicit, hand-reviewed `op.create_table` / `op.create_index` calls (generated by autogenerate against an empty DB, then reviewed — never trusted blindly). It includes the `BIGSERIAL`/`SERIAL` behaviour (SQLAlchemy emits it from `autoincrement` on a lone integer PK, the same path as `create_all`) and **no server defaults** (matching today's reality).
2. **No `drop`/`recreate` of existing tables anywhere.** The baseline is only ever *executed* on an empty database; on a populated database it is only *recorded* via `stamp`, and only after the pre-flight proves the database matches (§8).
3. `downgrade()` of the baseline **raises** (`RuntimeError("Baseline cannot be downgraded; restore from backup")`) so `alembic downgrade base` can never drop production tables.
4. The baseline represents the schema **as defined at T0** (two tables). Changes needed by T2 (users, identities, `user_id` columns, telegram link tokens) are later revisions — not folded into the baseline.
5. `create_all()` is **not** a long-term mechanism: it is kept only until each environment is adopted, then removed (§9, T1-G). **[REC]**

---

## 7. The four database states — handling matrix [REC]

These states are **not interchangeable** and must be detected, not assumed. Classification is performed by a read-only pre-flight (§8).

| State | Definition | Allowed action | Forbidden |
|---|---|---|---|
| **EMPTY** | no application tables, no `alembic_version` | `alembic upgrade head` (creates baseline) | — |
| **MATCH** | both tables exist and match metadata exactly (columns, types, nullability, PK, indexes), no `alembic_version` | after backup + owner approval: `alembic stamp 0001_baseline` (explicit revision id, **not** `head`), then `alembic upgrade head` for later revisions | stamping without a clean pre-flight report |
| **DIVERGENT / PARTIAL** | any difference: missing column/index/table, type or nullability mismatch, extra unmanaged tables/columns, only one of the two tables, etc. | stop; produce a diff report; owner decides a **reviewed corrective revision** (e.g. `ALTER TABLE ... ADD COLUMN user_email`, `CREATE INDEX`) applied *before* stamping, or manual fix then re-run pre-flight | automatic repair; `stamp`; `create_all` |
| **UNINSPECTABLE** | cannot connect, insufficient privileges, unsupported/odd schema (non-`public`, permission errors) | stop; no action; obtain credentials/permissions/evidence | assuming EMPTY or MATCH |
| **ALREADY VERSIONED** | `alembic_version` exists | compare recorded revision with the script directory; unknown revision ⇒ stop | `upgrade` when the recorded revision is not in the scripts |

Notes:
- An *empty database* and a *database that failed to open* look similar to careless tooling (both yield "no tables"); the pre-flight must distinguish a successful empty inspection from an exception. **[V-test]** (scenario A vs F below).
- "Partial" includes the plausible historical case of `agent_memories` lacking `user_email` and its index (§3.1).

---

## 8. Schema-drift detection and existing-database adoption

### 8.1 Pre-flight classifier (read-only)
**[REC]** A script (`migrations/preflight.py`, run as `python -m migrations.preflight`) that opens a connection with the settings URL, performs **inspector/catalog reads only**, prints a credential-free report and returns an exit code per state (0 EMPTY/MATCH-needs-stamp… exact mapping decided at implementation). It must never issue DDL/DML, never call `create_all`, and never print the URL.

Mechanics, proven locally on throw-away SQLite files **[V-test]**:

| Scenario | Result |
|---|---|
| A. empty database | `EMPTY` |
| B. created by `create_all` from current metadata | `MATCH` |
| C. divergent: `agent_memories` missing `user_email` and both indexes, `telegram_links` missing, an unmanaged `legacy_notes` table | `DIVERGENT` with each finding listed |
| C′. run `create_all` on C | **still `DIVERGENT`**; `agent_memories` columns unchanged (`create_all` does not alter tables) |
| E. type/nullability/index drift (`VARCHAR(50)`, nullable `agent_name`, missing indexes) | `DIVERGENT` listing type, nullability and index differences |
| F. database cannot be opened | `UNINSPECTABLE` (not `EMPTY`) |

Limits of that test: SQLite, not PostgreSQL; type-name comparison on PostgreSQL reflection (e.g. `TIMESTAMP WITHOUT TIME ZONE`, `SERIAL` defaults, PG-assigned PK names) is **[NV]**.

**[REC] Implementation choice:** use Alembic's own `compare_metadata` (via `MigrationContext`) as the primary diff engine, with `compare_type=True`, `compare_server_default=True`, plus explicit extra PostgreSQL checks Alembic does not make: that `agent_memories.id` and `telegram_links.chat_id` have `nextval(...)` defaults backed by sequences (`pg_get_serial_sequence`), PK constraint names, unmanaged tables, and that `alembic_version` is absent/known. Behaviour of `compare_metadata` against this schema is **[NV]** (Alembic not installed).

### 8.2 Existing-database adoption procedure (for the owner and CI, step by step) [REC]
1. **Freeze scope:** announce a short window; stop schema-changing deploys. The app may keep running during the read-only pre-flight, but `init_db()`/`create_all` must be disabled (or no new tables expected) before stamping.
2. **Backup:** `pg_dump -Fc` of the database; **restore it into a scratch database and verify** before proceeding. No stamp without a verified backup.
3. **Evidence capture (owner, read-only):** run the catalog queries in §12 and attach results (this is the *actual deployed state*).
4. **Pre-flight:** run `python -m migrations.preflight`; attach the report.
5. **Decision by state** per §7. For `MATCH`: owner approves, run `alembic stamp 0001_baseline`; verify `alembic current` shows `0001_baseline`; run `alembic upgrade head` (no-op until later revisions exist).
6. **For `DIVERGENT`:** write and review a corrective revision (separate from the baseline); test it on a restored copy of the backup; apply; re-run pre-flight until `MATCH`; only then stamp.
7. **Post-checks:** application smoke (history read, Telegram link read/write), pre-flight again (`ALREADY VERSIONED`, no diff).
8. **Record** the adoption (date, revision, operator) in `decision.md`/`flow.md`.

### 8.3 Fresh-database procedure [REC]
`alembic upgrade head` on an empty database (pre-flight says `EMPTY`), then application start. **Acceptance test (DB-free):** `alembic upgrade head --sql` output (offline mode) must contain DDL equivalent to the `CreateTable`/`CreateIndex` compilation in §3. **Acceptance test (DB-backed, optional):** a *disposable* PostgreSQL container created and destroyed by the test itself (random port and password, never the developer's existing instance), compare `pg_dump --schema-only` of `upgrade head` vs `create_all`. Neither is run in this task; whether a disposable PostgreSQL is available in CI/local is **[NV]** (Docker CLI exists on this machine, no CI exists).

---

## 9. Application integration and sequencing with T0 [REC]

- T0 introduced `init_db()` in `lifespan` (`main.py`) as a **transitional** call, and the T0 tests rely on it to create tables in SQLite. **[V-src]**
- **Phase 1 (T1-C…F):** add Alembic alongside; keep `init_db()` so nothing changes for running environments.
- **Phase 2 (T1-G, only after every environment is adopted):** replace the `create_all` call with a **schema-version check** in `lifespan`: compare `alembic_version` to the script head; in production a mismatch aborts startup with a credential-free message; in development/test it warns or auto-runs migrations on SQLite. Tests then build their SQLite databases via `alembic upgrade head` (also proving the migration chain), or via an explicit test helper.
- Migrations are **not** executed implicitly by the web process in production (no multi-replica races, no unexpected DDL at boot).

---

## 10. Rollback and recovery considerations [REC unless tagged]

- **Baseline `downgrade()` refuses** (see §6.3). Production policy is **forward-only**: corrections are new forward migrations; recovery from a bad migration is restore-from-backup.
- PostgreSQL DDL is transactional, so a failing migration normally rolls back entirely; exceptions (`CREATE INDEX CONCURRENTLY`, some `ALTER TYPE` operations) must be isolated in their own migration with an explicit note. **[NV for this project's future migrations]**
- `stamp` writes only the `alembic_version` table; undoing a mistaken stamp means deleting/updating that row (or `alembic stamp` to the correct revision). Exact CLI behaviour (e.g. purge options) is **[NV]**; the safe fallback is a manual, reviewed `DELETE` on `alembic_version`.
- Any future destructive step (drop column/table, type narrowing) must follow expand → migrate data → contract across separate releases, with backup and row-count assertions in the migration.
- A divergent production database may hold data in columns the models no longer define (extra columns/tables): corrective migrations must **not** drop them without owner sign-off and a backup. **[REC]**
- `pg_dump`-based backup/restore capability on the VM (disk space, retention, restore time) is **[NV]**.

---

## 11. Dependency assessment

| Item | Finding | Tag |
|---|---|---|
| Python | 3.11.9 (`backend/venv`) | V-test |
| SQLAlchemy | **2.0.51** installed | V-test |
| psycopg2-binary | 2.9.12 installed | V-test |
| typing_extensions | 4.15.0 installed | V-test |
| **Alembic** | **not installed** | V-test |
| Latest Alembic on the package index (queried today) | **1.20.1** | V-test (network query to the package index only) |
| Resolver dry-run (`pip install --dry-run alembic`, nothing installed) | would install `alembic-1.20.1`, `Mako-1.4.3`, `MarkupSafe-3.0.4`; requirements satisfied by existing `SQLAlchemy>=2.0` (2.0.51) and `typing-extensions>=4.12` (4.15.0); **no existing package would change** | V-test |

**[REC]** Pin `alembic==1.20.1` in `backend/requirements.txt` (runtime: migrations run at deploy time). Mako and MarkupSafe arrive transitively; pin them only if the owner wants fully reproducible installs (the rest of `requirements.txt` is unpinned, so a pin policy decision is separate — see T0-D6). Alembic's behaviour with this exact schema, with `compare_metadata`, and on PostgreSQL is **[NV]** until implemented and tested. Approval to modify the manifest and install is required (none done now). Note: querying the package index and the dry-run touched the network (package metadata only) and may have populated pip's cache outside the repository.

---

## 12. Evidence the owner must supply (actual deployed state) [REC — read-only queries]

To classify the deployed database the owner (or someone with access) runs these **read-only** statements and shares the output. No claims about the deployed schema are made until then.

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
Also needed: PostgreSQL version, whether other applications share this database, backup/restore tooling, whether the sample password from the README/git history was ever used in production, and who runs deployments.

---

## 13. Risks and unresolved questions

| # | Risk / question | Tag |
|---|---|---|
| R1 | Deployed schema may lack `user_email` and its index (history evidence) or otherwise diverge | NV / plausible |
| R2 | `telegram_links.chat_id` is `BIGSERIAL` in `create_all`-created DBs; a hand-made DB may differ; the baseline must match PostgreSQL reality, not only metadata | NV |
| R3 | Alembic `compare_metadata` accuracy on PG serial/identity, PK names and `TIMESTAMP` reflection | NV |
| R4 | `get_settings()` requires `GROQ_API_KEY`; tooling needs a DB-only settings accessor (or the model-module split) | V-src |
| R5 | `init_db()` and migrations coexisting could race on a fresh start; mitigated by sequencing (§9) | REC |
| R6 | Several replicas running upgrades concurrently; locking behaviour of Alembic itself | NV |
| R7 | Naming convention choice interacts with T2 foreign keys; needs owner decision before T2 | REC |
| R8 | T0 tests depend on `init_db()`; changing startup in T1-G requires test updates | V-src |
| R9 | Unpinned `requirements.txt`: a fresh install could pull different SQLAlchemy/Alembic combinations than tested | V-src |
| R10 | DB credential in git history equals the README sample password; production reuse unknown | NV |
| R11 | Disposable PostgreSQL for DB-backed acceptance tests may not be available; DB-free tests (offline SQL, SQLite) cover only part of the risk | NV |
| R12 | Timestamps are naive UTC; any later move to timezone-aware columns is a separate, data-affecting migration | V-src |
| R13 | Production backup/restore capability and downtime tolerance unknown | NV |

Open questions for the owner: Is the production database empty, populated, or already hand-altered? Who may run migrations and when? Is a maintenance window acceptable for adoption? Approve Alembic 1.20.1 pin, the `memory/models.py` split, and the naming convention?

---

## 14. Proposed implementation sequence with acceptance criteria [REC]

Each step is independently reviewable; none is started.

| Step | Scope | Acceptance criteria |
|---|---|---|
| **T1-B** Prerequisites | Owner decisions (§13); approval to add `alembic==1.20.1`; optionally the DB-evidence output from §12 | Written approval; if evidence is supplied, state classified |
| **T1-C** Models module + guard tests | Move `Base`/models to `memory/models.py`, re-export from `long_term.py`; optional naming convention; add `config.get_database_url()` | All existing 70 tests still pass; new guard tests (§4.3) pass; `git diff` shows models moved verbatim; schema DDL byte-identical to §3 (PG compile test) |
| **T1-D** Alembic scaffolding | Install pinned Alembic; `alembic.ini`, `migrations/env.py`, `script.py.mako` (no revisions yet); `include_object`; no URL in files/logs | `alembic --help`/`heads` work; offline mode emits no credentials; test asserts a sentinel password never appears in stdout/stderr/log output of success **and** failure paths; no `alembic.ini` URL |
| **T1-E** Baseline revision | `0001_baseline` hand-reviewed; refusing `downgrade()` | `upgrade head --sql` DDL equals metadata DDL; SQLite `upgrade head` then `compare_metadata` returns no diffs; `downgrade base` raises; single head; no `DROP` anywhere in upgrade |
| **T1-F** Pre-flight classifier | `migrations/preflight.py` read-only | Tests (SQLite) for EMPTY, MATCH, DIVERGENT (missing column/index/table, type/nullability, extra table), UNINSPECTABLE, ALREADY VERSIONED known/unknown revision; asserts only inspector/SELECT statements are issued (statement log inspection); credential-free output; (optional) disposable-PG parity test |
| **T1-G** Adoption runbook | Written runbook from §8.2; owner executes steps against real environments | Verified backup restore evidence; pre-flight report attached; `alembic current` = `0001_baseline` in each environment; app smoke checks pass |
| **T1-H** Startup switch | Replace `create_all` in `lifespan` with schema-version check; remove `init_db()`; update tests to use migrations | Production startup fails clearly on version mismatch without leaking credentials; dev/test behaviour documented; full suite green; `flow.md`/`decision.md` updated |

Exclusions that continue to apply to T1 as a whole: no users/identity/JWT (T2+), no route changes, no Telegram/Twilio changes, no Docker/CI/deployment automation, no destructive schema changes.

---

## 15. Assumptions made in this plan
1. The T0 code is the schema authority for what the application expects (**[V-src]**), not necessarily what is deployed (**[NV]**).
2. A single PostgreSQL database in the default `public` schema is used, with `postgresql://` URLs and the psycopg2 driver (**[V-src]** from settings and requirements).
3. The owner will accept running migrations as a separate deploy step rather than at app startup (**[REC]**, needs approval).
4. Alembic 1.20.1 behaves as documented against SQLAlchemy 2.0.51 on PostgreSQL (**[NV]**).
5. SQLite-based tests are adequate for structural checks only and cannot validate PostgreSQL-specific details (**[V-test]** limitation).

---

## 16. Scope confirmation for this task
- Inspection used: reading source and git history, SQLAlchemy DDL compilation without any connection, throw-away SQLite files in a temporary directory, a package-index version query and a pip dry-run.
- **No connection to any PostgreSQL or Redis instance was attempted**, and socket connection was disabled in the scripts that imported the model module.
- No schema or data was created or modified in any real database; no file in the repository other than this document was created or changed.

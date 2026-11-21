# Master context append: sprint 5 (final completion programme)

- Product is now a shared-workspace product: members read all workspace plans/runs/usage; only the creator/starter writes; approvals by owner/admin; invitations for people who have not signed in (migration 0017, hashed single-use expiring token, verified-email match, uniform failure, no e-mail delivery yet). Created-by/started-by are shown as e-mails.
- Found and fixed while doing it: run cancel was authorised through the read path (privilege escalation); `PlanRunStore.require_owner` now guards it.
- Tool matrix test covers every registered tool; the Studio tool page shows the delivery guarantee. Real Resend send still unverified; `demo.send_note` is simulated and labelled.
- Gates on this tree: backend 80 files / 1,421 passed (sequential), PostgreSQL 64 passed, real-Redis coordination 16 passed, Redis drill PASS, frontend 148 passed / 6 skipped, tsc/eslint/actionlint clean, browser journey verified, axe 0 violations. Not run: GitHub CI, staging, production, live model, screen reader.
- D069-D074 remain "Directed, owner confirmation pending" (`docs/decision-confirmation.md`); D046/D050 untouched; `decision.md` not edited.
- Open: invitation e-mail delivery (needs Resend sandbox key), invitation TTL not configurable, no owner transfer, UI shows write buttons to non-creators (server refuses with 404; not hidden yet), invitation logic tested on SQLite only (migration verified on PostgreSQL).

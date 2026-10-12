# 2026-10-12 - one-off admin close of legacy (no master_trade_id) follower positions

Branch `claude/admin-close` (cut from `origin/main` 323d091). NOT merged, NOT deployed: the coordinator deploys after review.

## Why
Two active followers hold an ADA/USD:USD LONG (906 and 899 contracts) opened 2026-10-11 19:00 UTC from a LEGACY master
BUY signal. Legacy signals have no `master_trade_id`, so `POST /api/broadcast-close` (the only match is the
`master_trade_id` join) can never close them. The owner wants them closed.

## What
- `follower_endpoints.py`: `POST /api/admin/close-legacy-positions` (`verify_master_key`). Selects open/needs_recovery
  rows whose signal exists with `master_trade_id IS NULL`, matching symbol + side (BUY=long, SELL=short), active users
  only. Refuses (422, nothing written) on a count != `expected_count`, on a bad/extra body field, on any T1 row in the
  selection (defence in depth) or on a row that already has a close job from another close; 409 if a row changed
  between the select and the insert; 410 always from 2026-10-19T00:00:00Z.
- Real call = ONE transaction: a `master_closes` row (`reason='admin_legacy_close'`, side BUY/SELL, id = first 32 hex of
  sha256 over 'admin-legacy-close|symbol|side|sorted position ids') + one pending `close_jobs` row per matched row.
  A repeat of the same selection finds the master_closes row -> `duplicate:true`, nothing new.
- The executor (`hosted_trading_loop.execute_close`) is UNCHANGED and runs the jobs: cancel TP by id, `exit_hint='TIME'`,
  ONE reduce-only market close of min(venue, row), verified flat, cancel SL by id. `CLOSE_JOBS_ENABLED=0` holds the
  jobs (they are still recorded; the response says `close_jobs_enabled:false`).
- Events: `CLOSE_RECEIVED` / `CLOSE_JOB_CREATED` / `CLOSE_DUPLICATE` with `reason='admin_legacy_close'`. Only the first
  8 chars of a user key are ever returned or logged.
- No schema change, no `main.py` / `follower_models.py` / shared-file change. `tools/run_gates.py` lists the new test.
- Tests: `tests/test_admin_legacy_close.py`.

## Runbook (never print MASTER_API_KEY; run from a shell where it is set in the env)
1. Dry run (writes nothing; check 2 rows, key prefixes nk_U8uPZ / nk_8ocDp, qty 906 / 899):
   `curl -sS -X POST "$BASE/api/admin/close-legacy-positions" -H "X-Master-Key: $MASTER_API_KEY" -H "Content-Type: application/json" -d '{"symbol":"ADA/USDT","side":"long","dry_run":true,"reason":"legacy_rollback_cleanup","expected_count":2}'`
2. Execute (same body, `"dry_run":false`):
   `curl -sS -X POST "$BASE/api/admin/close-legacy-positions" -H "X-Master-Key: $MASTER_API_KEY" -H "Content-Type: application/json" -d '{"symbol":"ADA/USDT","side":"long","dry_run":false,"reason":"legacy_rollback_cleanup","expected_count":2}'`
3. Expect `jobs_created:2`; the trading loop runs them next cycle (check `logs/close_events.jsonl` for `CLOSE_JOB_OUTCOME` done). A second call says `duplicate:true`.
4. If `close_jobs_enabled` is false, set CLOSE_JOBS_ENABLED=1; jobs older than 24 h expire.
5. After both jobs are `done` (monitor marks rows closed), the endpoint can stay; it answers 410 from 2026-10-19.

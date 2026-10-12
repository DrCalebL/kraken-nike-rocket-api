# Admin close of legacy follower positions (reusable, NOT on main)

**Use when** followers hold positions opened from a LEGACY master signal (no `master_trade_id`), and the owner wants
them closed. `POST /api/broadcast-close` can never close these: its only match is the `master_trade_id` join, by design.

**Where the code is:** branch `claude/admin-close` @ `208b1a4`. It adds `POST /api/admin/close-legacy-positions` to
`follower_endpoints.py`, 34 tests in `tests/test_admin_legacy_close.py`, and the progress doc
`docs/progress/2026-10-12-admin-close-legacy.md`. It is deliberately kept off main: it must be live only while it is needed.

**History:** built and reviewed (Opus: READY TO DEPLOY) on 2026-10-12 for two ADA longs opened by the legacy algo during
the 2026-10-11 auto-rollback. It was deployed at 01:51Z, but both positions had already stopped out at 01:49Z. Its dry
run correctly refused (`matched 0, expected_count 2`), and it was reverted at `04ebff5` the same hour.

## What it does (safety properties)
- Auth is `X-Master-Key` (`verify_master_key`). 401/503 otherwise.
- It selects only `open`/`needs_recovery` rows whose signal has `master_trade_id IS NULL`, matching symbol and side,
  for active users. It never selects by time.
- It refuses with 422 and writes nothing when:
  - the matched count differs from `expected_count`;
  - a T1 row is in the selection;
  - a row already has a close job;
  - a body field is extra or bad.
- It returns 409 if a row changes mid-call, and 410 always after a hard-coded expiry.
- `dry_run:true` writes nothing and lists the rows: position id, the first 8 chars of the user key, qty, entry, and
  whether SL/TP ids exist.
- A real call is one transaction: a `master_closes` row (`reason='admin_legacy_close'`, a deterministic id) plus one
  `close_jobs` row per position. A repeat call returns `duplicate:true`.
- The UNCHANGED close executor runs the jobs: cancel TP by id, then one reduce-only market close, then verify flat,
  then cancel SL by id. It never cancels all orders.

## How to reuse (each step needs the owner's yes)
1. Rebase the branch: `git checkout claude/admin-close && git rebase origin/main`.
2. Set `ADMIN_LEGACY_CLOSE_EXPIRY` in `follower_endpoints.py` to about one week from today.
3. Change the selection if needed (symbol / side are body fields; anything else is a code change and needs new tests).
4. Consider fixing known limit 1 below first, then run `python tools/run_gates.py`.
5. Get a fresh Opus money-safety review.
6. Fast-forward main. Railway auto-deploys, so check the deployment reaches SUCCESS.
7. Run the dry run from a shell holding `MASTER_API_KEY` in its env (the Kraken master host:
   `bash -ic`, `$FOLLOWER_API_URL`). Never print the key:
   `curl -sS -X POST "$FOLLOWER_API_URL/api/admin/close-legacy-positions" -H "X-Master-Key: $MASTER_API_KEY" -H "Content-Type: application/json" -d '{"symbol":"ADA/USDT","side":"long","dry_run":true,"reason":"legacy_rollback_cleanup","expected_count":N}'`
8. Show the owner the rows. On their yes, send the same body with `"dry_run":false` immediately, and check that the
   response's position ids equal the dry run's.
9. Watch `CLOSE_JOB_OUTCOME` = done in the logs. If `close_jobs_enabled` is false, nothing runs; jobs expire after 24 h.
10. Revert the commit on main once the jobs are done.

## Known limits (review 2026-10-12, all LOW)
1. `expected_count` checks the count, not the ids. Running the execute right after the dry run keeps the gap small.
   Better: add an `expected_position_ids` field.
2. If the jobs expire or fail, a repeat call returns `duplicate` and cannot retry. Close by hand on Kraken instead.
3. The monitor records these closes as `TIME` exits (`exit_hint='TIME'`), which is cosmetic.

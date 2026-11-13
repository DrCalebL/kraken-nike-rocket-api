# 2026-11-13 T1LIVE-7 follower side: BNB hold + leader_exit (branch claude/t1-live-7)

Built to the contract in the coordinator brief (spec s3 + s9 "same as HL vault"). Not deployed; HOLD_ENABLED stays 0.

- New `hold_guard.py` (validation, G1c sizing at the follower mark, 10x/margin shrink, allowlist), `POST /api/broadcast-hold`,
  additive migrations (inside the T1-CLOSE marker block of `main.py`), models, `execute_hold_entry`, slot logic, serialised
  entries, kind-aware guardian/monitor, symbol-scoped `check_position_closed`, `leader_exit` close reason (`close_guard`,
  shared with hyperliquid-nike-rocket-api, fixture `close_v2.json` + `test_close_common.py` updated).
- Tests: `test_hold_guard.py`, `test_strategy_slots.py`, `test_hold_entry.py`, `test_hold_rows.py`.
- Replaced: `test_a_second_concurrent_execution_keeps_the_user_in_flight` (execute_trade now refuses while in flight).
- Unverified on the real venue: PF_BNBUSD lot (fake uses 0.01) and tick (0.1); free-margin field of the flex account
  (`info.accounts.flex.availableMargin`, else `USD.free`; unreadable -> hold skipped); cross vs isolated margin;
  width of `trades.exit_type` for the new label `LEADER`.

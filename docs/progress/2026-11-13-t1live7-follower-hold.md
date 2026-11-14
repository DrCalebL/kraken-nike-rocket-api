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

## Pre-deploy check (run on the production DB before the first deploy of this branch)
`SELECT master_trade_id FROM signals WHERE master_trade_id IS NOT NULL GROUP BY 1 HAVING count(*) > 1` must return NO rows.
Otherwise the `ux_signals_master_trade_id` unique-index migration cannot be built and (it is essential) aborts every boot.

## Same Binance hold, second master_trade_id
A master that lost its state can mint a new `master_trade_id` for the same Binance hold. `/api/broadcast-hold` answers 409
`HOLD_DUPLICATE_LEADER_TRADE` (error_logs row + admin alert) for a NEW id whose `leader_trade_id` already has a hold signal;
delivery skips, and `execute_hold_entry` refuses (acknowledging the delivery), any user with an open / needs_recovery hold
row whose signal has the same `hold_leader_trade_id`.

## Audit round (Fable multi-audit of 7d8dc9c)
- Leverage per hold: `lev = min(10, floor(0.8 / (d + HOLD_MM + HOLD_FUNDING_ALLOW)))`, `d = (P - stop) / P` at the follower's own
  mark, `HOLD_MM` default 0.025, `HOLD_FUNDING_ALLOW` default 0.02 (`hold_guard.LIQ_BUFFER`). ASSUMPTION (unverified on the venue):
  an isolated long liquidates at about -(1/lev - MM) of price; the stop plus MM plus funding must sit inside 80% of that
  distance. **Verify PF_BNBUSD's maintenance margin on the Kraken demo venue BEFORE HOLD_ENABLED=1** (and tune HOLD_MM). `lev < 1` refuses (`HOLD_STOP_TOO_WIDE`). The margin fit uses that lev and
  `set_leverage(lev)` runs before the entry (a failure = no entry). The row stores the chosen leverage.
- Entry order of operations: all read-only checks (slot, balance, ticker, size, set_leverage) -> atomic acknowledge -> a
  `needs_recovery` hold row (`kind='hold'`, target_tp NULL, planned qty, target_sl) -> entry -> confirmed fill -> SL -> promote the
  row to `open` (3 tries). A crash anywhere after the row leaves a row the guardian's recovery lane acts on (naked position closed at
  market; a never-filled entry ends `closed_manual`). A promotion that cannot be written closes the position at market and cancels
  the stop (a stop that cannot be cancelled keeps the row `needs_recovery`, and the stop's id is written onto the recovery row the
moment it is placed). A pre-entry row that never got an entry order (a failed or crashed attempt that never traded) does not
block a later re-offer; a row that traded does. A definitive refusal acknowledges the delivery; an unreadable state leaves it open for the next cycle. A hold attempt
  that fails after the row was written ends `closed_manual`, which (like any row of that trade) blocks a later re-offer of the SAME
  master_trade_id for this user: a transient failure costs that offer, never a double entry.
- `HOLD_ENABLED` is effective only with `GUARDIAN_MODE=enforce` (the hold SL repair exists only there); otherwise the hold stays
  off and `HOLD_NEEDS_GUARDIAN_ENFORCE` is raised at startup and each cycle (hourly dedupe). The endpoint skips offers likewise.
- The rowless legacy scan counts tracked rows per (user, symbol). The guardian's hold-symbol exemption (never closing a hold
  symbol by the rowless rule) is kept: with enforce it is redundant (rowed symbols are skipped anyway) and only matters for a
  hold row left over after a mode change.
- position_monitor re-reads `exit_hint` / `close_fill_qty` after a confirmed flat and defers the label while a master close is
  still running (hint set, no fill, user in flight or its job pending / done < 180 s).

## Deploy runbook (checks run BEFORE merge: Railway auto-deploys on merge)
1. Pre-deploy queries on the production DB, each must return NO rows / zero:
   - `SELECT master_trade_id FROM signals WHERE master_trade_id IS NOT NULL GROUP BY 1 HAVING count(*) > 1` (else the unique
     index migration aborts every boot).
   - the late-close sweep: `SELECT op.id FROM open_positions op JOIN signals s ON op.signal_id = s.id JOIN master_closes mc ON
     mc.master_trade_id = s.master_trade_id LEFT JOIN close_jobs cj ON cj.position_id = op.id WHERE op.status IN ('open',
     'needs_recovery') AND cj.position_id IS NULL` (else the first cycle queues close jobs for those rows).
2. Apply the hold DDL BY HAND in a quiet minute (psql, `SET lock_timeout = '5s'`, the statements of `_HOLD_MIGRATIONS` and the
   CLOSE block), then deploy: the boot migrations then take no table lock (they skip what exists).
3. Deploy or change env (`HOLD_ENABLED`, `GUARDIAN_MODE`, `HOLD_USER_ALLOWLIST`) outside 23:55-00:30Z (daily hold offers /
   re-offers) and outside HH:58-HH:05 (ADA entries); nothing else restarts the loop then.
4. No rollback to an older image while any `kind='hold'` row is open or `needs_recovery`: the old code does not know the kind
   and would treat a TP-less row as an unprotected position.
5. Canary first: `HOLD_ENABLED=1`, `GUARDIAN_MODE=enforce`, `HOLD_USER_ALLOWLIST=<one or two follower ids>`. For those users the
   ADA auto-raised leverage is capped at 10x (never below the signal's own leverage).

# 2026-06-16 — Reduce-only bracket race (unprotected positions)

Status: **fixed & pushed** to `claude/gallant-allen-0i4eov` (both repos), QC'd by a
3-agent review wave, validated locally. **Not deployed** — review + paper/small-live
test before production.

## Symptom
Two ADA shorts (user 4 `nk_U8uPZTS6eRLz` ≈ caleb@nikepig.com, user 6
`nk_8ocDpJrYakpM`) appeared to run **unprotected**. DB showed every `open_positions`
row `status='closed'`, `db_position=0`, and **no** `needs_recovery` rows.

## Investigation (what the evidence showed)
- Kraken **Positions** history for the inspectable account (user 4, UI in UTC+8):
  every ADA position had opened *and* closed (SL/TP firing; ~−2% stop-outs, some
  TP gains). DB `closed` matched the exchange — **no naked position remained**.
- One Kraken position had **no DB row**: 6/16 ~04:00 UTC, ~1,752 ADA, closed ~flat.
- Kraken **Orders** log for that 1,752: `Sell` market fill (open) → Buy **Limit**
  reduce-only **Rejected ×3** (TP) → Buy **Market Rejected ×5** → Buy market fill
  (force close). The rejected TP detail showed **Reduce only: Yes**, `Quantity: –`.
  Healthy positions instead showed a resting TP limit cancelled on close, no rejects.

## Root cause (confirmed in code + by 2 verification agents)
The bot placed the reduce-only TP/SL after only a blind `asyncio.sleep(2)`
(`hosted_trading_loop.py:793`), with **no confirmation the entry fill was
registered**. Under Kraken latency the position wasn't yet visible, so Kraken
rejected the reduce-only orders ("would not reduce position"). Retries re-submitted
blindly (TP ×3 → emergency market ×5). The **same failure path** then tried to
record a `needs_recovery` row — but pre-`fd723fa` that insert omitted the NOT NULL
`target_tp`/`target_sl` columns and **threw**, leaving the position untracked (the
"ghost"). Hence `db_position=0` *and* `needs_recovery=0`.

- `fd723fa` (2026-06-16 05:38 UTC, ~1.5h **after** the 04:00 incident) already fixed
  the NOT-NULL crash → failures now persist a `needs_recovery` row. It did **not**
  fix the race (the cause). The guardian's `db_position`-skip theory and a
  "closed-while-open" DB-mislabel theory were both **red herrings**.
- The ghost was a **follower** trade (it's in user 4's own Kraken log), not a master
  trade — H1 considered and ruled out.

## The fix
First attempt `f5c2bdc` was **broken** (caught by the QC wave): it used
`fetch_positions([symbol])`, which on krakenfutures ignores `PF_` ids and returns
`[]` → confirmation never fired, and `_position_is_open` reported live positions as
**flat**, causing the emergency close to be skipped (strictly worse). Corrected in
`7fc18d8`:

- `_get_open_position` — `fetch_positions()` **no-arg** + unified-symbol match
  (via `exchange.market(symbol)['symbol']`, base-substring fallback), offloaded with
  `asyncio.to_thread` so it doesn't stall the `gather()` batch.
- `_wait_for_position` — polls `_get_open_position` (timeout 8s, poll 1s) before the
  bracket; dropped the unsupported `fetch_order` fallback; returns entryPrice.
- `_position_is_open` — now `async`; awaited in `_emergency_close_position`, which
  skips when already flat and stops retrying once flat.
- Seed `entry_fill_price` from the confirmed position (removed a dead, always-failing
  `fetch_order`). Warn on partial fills (sizing still uses requested qty — see
  follow-ups).
- Tests: `tests/test_wait_for_position.py` (9 DB-free scenarios). Validated locally
  by stubbing `ccxt`/`aiohttp` (pytest not installed in sandbox): **9/9 pass**.

Master repo (`massive-rocket-algos`, commit `ff61e6b`): same race after a blind
`sleep(3)` + (unsupported) `fetch_order` "continue anyway". Ported module-level sync
`_get_open_position` / `_position_is_open` / `_wait_for_position`; wired into the
entry path and `_emergency_close_position`. Validated 9/9.

## QC wave findings & resolution
- **BLOCKER** `fetch_positions([symbol])` returns `[]` for `PF_` ids → fixed (no-arg + match).
- **BLOCKER** `_position_is_open` reported live positions as flat → fixed by the above.
- **MAJOR** sync ccxt calls block the event loop in the gather batch → fixed with `asyncio.to_thread`.
- **MAJOR** `fetch_order` is `NotSupported` on krakenfutures → removed; position is the confirmation signal.
- **MINOR** wired the confirmed fill price; tightened timeout 15→8s; `poll_interval` guard; partial-fill warning.

## Known pre-existing issues discovered (NOT changed — scope)
- `hosted_trading_loop.py:415` `check_existing_position` still uses the broken
  `fetch_positions([symbol])` → dedup likely never sees a live position. Fixing it
  changes dedup behavior; do deliberately.
- The old entry fill-price `fetch_order` was a no-op (NotSupported) → DB stored the
  signal price, not the actual fill. The fix now uses the confirmed position's price.

## Follow-ups
- Size reduce-only TP/SL to the **confirmed** contracts on partial fills.
- Consider wrapping the remaining sync ccxt calls in `hosted_trading_loop.py`
  (entry/TP/SL/emergency `create_order`) in `asyncio.to_thread` for full batch concurrency.
- Optional: explicit `ccxt.RateLimitExceeded`/`DDoSProtection` backoff.

## Deploy / verify
1. `pip install -r requirements.txt -r requirements-test.txt` then `pytest tests/test_wait_for_position.py`.
2. One small live trade on the owner's own account; expect log
   `✅ Position registered (...) after N check(s)` and fewer `EMERGENCY_CLOSE` events.
3. Watch `error_logs` for reduce-only rejections dropping to ~zero.

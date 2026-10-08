# CLAUDE.md — kraken-nike-rocket-api

> Keep this file lean (it loads into every session). Detailed history lives in
> `docs/progress/` — read those on demand, don't paste them here.

<!-- FULL-WORKFLOW:START -->
## Full workflow (Opus 5.5, multi-agent)
Before each wave, reread root `CLAUDE.md`. The coordinator runs Claude Opus 5.5 (`claude-opus-5-5`). To save credits (owner 2026-10-08), builders, integrators/mergers and recon/explore agents run Sonnet 5.5 (`claude-sonnet-5-5`); planners and plan judges, adversarial reviewers (always the money-safety lens) and QC stay on Opus 5.5.
Routine work: the coordinator builds, then one fresh, independent, read-only reviewer checks it.
"Full workflow" (high-risk or explicitly requested) runs five stages in order, scaled to the task:
1. **Plan** — several independent planners converge on an exact consensus spec BEFORE any code.
2. **Build** — builders implement strictly to the spec (disjoint files, or worktrees where they would overlap).
3. **Review** — fresh adversarial reviewers on independent lenses (correctness, coverage, regression, security/money-safety, voice).
4. **QC** — the coordinator re-verifies every finding, fixes each worth fixing (nits too when cheap and correct), reruns affected gates, and sends material fixes to fresh review.
5. **Deliver** — CLAUDE.md for durable facts only + a dated `docs/progress/` entry; commit, fetch, rebase on current `origin/main`, rerun gates, push only the feature branch (`--force-with-lease` only after rewriting a published branch).
Keep this block identical in the root `CLAUDE.md` of all four rocket repos (massive-rocket-algos, kraken-nike-rocket-api, hyperliquid-nike-rocket-api, hyperliquid-vault-rocket-api) and log each touched repo in its `docs/progress/`.
<!-- FULL-WORKFLOW:END -->

## What this is
$NIKEPIG's Massive Rocket — a Kraken **Futures** copy-trading service. This repo is
the **follower / hosted API**: it polls trade signals and mirrors each onto every
active follower's Kraken sub-account, tracking positions in Postgres.

- Sibling repo **`massive-rocket-algos`** = the **master / signal generator**
  (trades its own account, **no Postgres** — local list + `kraken_tpsl_orders.json`).
  It has the same exchange gotchas; fixes often need to land in both.
- Runtime: FastAPI on Railway. Entry point `main.py`; the trading loop is a
  background task in `hosted_trading_loop.py`.

## Key modules
- `hosted_trading_loop.py` — polls signals, executes the **SL-first bracket** per user, records
  `open_positions`. Batches users 25 at a time. Dedup (`check_any_open_positions_or_orders`) **fails closed**:
  a read error skips the trade. A market entry is never re-sent after an ambiguous error.
- `order_utils.py` — retry wrappers (`place_*_with_retry`, `MAX_RETRIES=3`) + admin email.
- `position_monitor.py` — tracks open positions, marks them `closed`/`needs_review`. Only it changes `'open'` rows.
- `position_guardian.py` (v2.0, Wave 1) — two-leg invariant for service-opened positions, on its own thread
  pool (`GUARDIAN_MAX_CONCURRENCY`, default 8). `GUARDIAN_MODE=legacy|shadow|enforce`, **default shadow**:
  legacy = v1 plus safety edits; shadow = legacy plus log-only `GUARDIAN_WOULD_<KIND>`; enforce = acts.
  Rowless positions keep today's close rule. Roll back with `GUARDIAN_MODE=legacy`.
- Shared Wave 1 modules: `bracket_guard.py` (byte-identical to `massive-rocket-algos`), `follower_guard.py`
  (row policy, in-flight tokens, flat/fills evidence) and `signal_guard.py` (broadcast validation), both
  byte-identical to `hyperliquid-nike-rocket-api`. Never edit one copy alone; gates compare sha256.
- `follower_endpoints.py` — broadcast payload validated by `signal_guard` (422) before any DB write;
  `risk_pct` required, `0 < risk_pct <= 0.03`. `MASTER_API_KEY` and `ADMIN_PASSWORD` have **no default**:
  unset -> 503; compared with `hmac.compare_digest`.
- `db_utils.py`, `billing_service_30day.py`, `balance_checker.py`, `price_cache.py`,
  `portfolio_api.py`.
- DB (asyncpg/Postgres): `open_positions`, `follower_users`, `signals`, `error_logs`,
  `trades`, `position_fills`, `billing_*`.

## ⚠️ Exchange gotchas (CCXT `krakenfutures`) — verified, high-impact
1. **The client is SYNCHRONOUS.** In `async` code, wrap blocking calls:
   `await asyncio.to_thread(exchange.fetch_*)`. Pattern used in `balance_checker.py`,
   `price_cache.py`, `portfolio_api.py`. Do **not** block the event loop inside the
   `asyncio.gather()` user batches.
2. **`fetch_positions([symbol])` ignores native `PF_` ids** (it filters on the
   *unified* symbol e.g. `ADA/USD:USD`) → returns `[]`. **Always call
   `fetch_positions()` no-arg and match client-side** (see `_get_open_position`,
   `position_monitor.py:850`). The old `check_existing_position` still uses the broken form but is never called.
3. **There is no `fetch_order`** (raises `NotSupported`). Use `fetch_open_orders` /
   `fetch_closed_orders` / `fetch_my_trades`, or read the position itself.
4. **Reduce-only TP/SL must be placed only AFTER the position is registered.** A
   reduce-only order sent before the entry fill registers is rejected
   ("would not reduce position"). Confirm via `_wait_for_position` (polls the
   position) — never a blind `sleep`. This was the 2026-06-16 incident.
5. `open_positions.target_tp` / `target_sl` are **NOT NULL** — the recovery insert
   must populate them (fixed in `fd723fa`).
6. Open-order rows carry the SL level in `info['stopPrice']`, not unified `triggerPrice`. One empty positions
   read is never proof of flat: a flat counts only after a confirming re-read.

## Bracket flow (the critical path)
entry market order → **`_wait_for_position` confirm** → reduce-only **SL (stop-market) first**, sized to the
confirmed fill → TP (limit) → insert `open_positions (status='open')`. SL failure → `_emergency_close_position`.
TP failure: enforce keeps the SL (guardian places the TP); other modes close first and cancel the SL only after
`_verified_flat`. Every exit after the entry may have filled that leaves no bracket + row and no verified flat
writes `_record_unprotected_position (status='needs_recovery')` with the levels and leg ids (reasons such as
`SL_FAILED_CLOSE_UNVERIFIED`, `TP_FAILED_CLOSE_UNVERIFIED`, `LEFTOVER_SL_CANCEL_FAILED`, `POST_ENTRY_EXCEPTION`).
The guardian closes or restores those rows; `closed_manual` only after 240 s of confirmed flat. Symbols map via
`SYMBOL_MAP` (API `ADA/USDT` → Kraken `PF_ADAUSD`).

## Testing and gates
- Gate: `pip install -r requirements-gate.txt` (CPython 3.13; not the production set), then
  `python tools/run_gates.py` must print `GATES PASS` (0 failures, errors, skips). Tests run offline: the
  network is blocked and production credentials are scrubbed. Alternative env: py3.11 venv with
  `requirements.txt` + `requirements-test.txt`.
- Offline fakes in `tests/fakes/` (`fake_krakenfutures.py`, `fake_pool.py`). `test_billing_integration.py`
  needs `TEST_DATABASE_URL` and is outside the gate.
- `tools/chaos_follower.py` is owner-run only, against the Kraken demo venue.
- Always `python -m py_compile <file>` after edits.

## Conventions
- Logging uses `   ` (3-space) indented messages with emoji prefixes (✅ ⚠️ ❌ 🚨 📊).
- Develop on the wave's feature branch (`claude/<wave>`); commit + push only that branch;
  do **not** open PRs unless asked. This is a **live money** system — verify before deploy.
- Alarms: `error_logs` rows named `GUARDIAN_<CODE>` / bare entry-path names (`NEEDS_RECOVERY_RECORDED`,
  `EMERGENCY_CLOSE_FAILED`, ...) + admin email for critical codes. Query with the stored name.

## History
See `docs/progress/` (e.g. `2026-06-16-reduce-only-bracket-race.md`, `2026-09-28-wave1-phase0-safety.md`).

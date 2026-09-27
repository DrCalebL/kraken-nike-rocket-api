# CLAUDE.md — kraken-nike-rocket-api

> Keep this file lean (it loads into every session). Detailed history lives in
> `docs/progress/` — read those on demand, don't paste them here.

<!-- FULL-WORKFLOW:START -->
## Full workflow (Opus 5.5, multi-agent)
Before each wave, reread root `CLAUDE.md`. The coordinator and every worker run Claude Opus 5.5 (`claude-opus-5-5`).
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
- `hosted_trading_loop.py` — polls signals, executes the **3-order bracket**
  (entry → TP → SL) per user, records `open_positions`. Batches users 25 at a time.
- `order_utils.py` — retry wrappers (`place_*_with_retry`, `MAX_RETRIES=3`) + admin email.
- `position_monitor.py` — tracks open positions, marks them `closed`/`needs_review`.
- `position_guardian.py` — periodically scans the **exchange** and DB for unprotected
  positions and force-closes them (last safety net).
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
   `position_monitor.py:850`).
3. **There is no `fetch_order`** (raises `NotSupported`). Use `fetch_open_orders` /
   `fetch_closed_orders` / `fetch_my_trades`, or read the position itself.
4. **Reduce-only TP/SL must be placed only AFTER the position is registered.** A
   reduce-only order sent before the entry fill registers is rejected
   ("would not reduce position"). Confirm via `_wait_for_position` (polls the
   position) — never a blind `sleep`. This was the 2026-06-16 incident.
5. `open_positions.target_tp` / `target_sl` are **NOT NULL** — the recovery insert
   must populate them (fixed in `fd723fa`).

## Bracket flow (the critical path)
entry market order → **`_wait_for_position` confirm** → reduce-only TP (limit) +
SL (stop) → insert `open_positions (status='open')`. On TP/SL failure →
`_emergency_close_position` (reduce-only market; skips when already flat) → if all
retries fail → `_record_unprotected_position (status='needs_recovery')` for the
guardian. Symbols map via `SYMBOL_MAP` (API `ADA/USDT` → Kraken `PF_ADAUSD`).

## Testing
- `pytest` (`pytest.ini`: `asyncio_mode=auto`); tests in `tests/`.
- Most integration tests need `TEST_DATABASE_URL`. DB-free example:
  `tests/test_wait_for_position.py`.
- Fresh sandboxes may lack `ccxt`/`pytest` — `pip install -r requirements.txt
  -r requirements-test.txt` first, or stub `ccxt`/`aiohttp` for pure-logic checks.
- Always `python -m py_compile <file>` after edits.

## Conventions
- Logging uses `   ` (3-space) indented messages with emoji prefixes (✅ ⚠️ ❌ 🚨 📊).
- Develop on the wave's feature branch (`claude/<wave>`); commit + push only that branch;
  do **not** open PRs unless asked. This is a **live money** system — verify before deploy.

## Known pre-existing issues (not yet fixed — see progress log)
- `hosted_trading_loop.py:415` (`check_existing_position`) still uses the broken
  `fetch_positions([symbol])` form → dedup may not see live positions.

## History
See `docs/progress/` (e.g. `2026-06-16-reduce-only-bracket-race.md`).

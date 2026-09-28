"""
Nike Rocket - Position Guardian v2.0 (Wave 1 / Phase 0)
============================================================================
Keeps every service-opened Kraken Futures position inside the two-leg bracket
(one stop-market SL at the recorded level covering the position, one TP at the
recorded level for the position qty).

GUARDIAN_MODE (env, read at start; follower_guard.guardian_mode, default 'shadow'):
- legacy : v1 behaviour (startup scan, needs_recovery closes every 120 s,
           exchange scan every 5th cycle) plus the Wave 1 safety edits.
- shadow : legacy acting + the invariant every 2nd cycle, log-only
           (GUARDIAN_WOULD_<KIND>), zero invariant mutations.
- enforce: the invariant every cycle over users with an active row, a full
           pass every 2nd cycle, today's rowless rule every 5th cycle.

Safety rules (spec I1-I19): orders read FIRST, positions SECOND, both no-arg on
the guardian's own thread pool; a failed read never acts (the 3rd failed read of
a user in a row raises GUARDIAN_BLIND); the guardian acts only on a (user,
symbol) with exactly one consistent active row; it cancels only service-placed
orders; an 'open' row whose symbol is flat is left to position_monitor; one
flat read is never a close or a confirmed flat; a new SL is confirmed before
the old one is cancelled; a TP problem never closes; an in-flight execute_trade
(token or acknowledged_at) always wins.

Author: Nike Rocket Team
Created: March 19, 2026 - v2.0 Wave 1: September 28, 2026
"""

import asyncio
import functools
import json
import logging
import os
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional, Dict

import ccxt
from cryptography.fernet import Fernet

import bracket_guard as bg
import follower_guard
from order_utils import notify_admin

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('POSITION_GUARDIAN')



def _env_int(name, default, lo, hi):
    """QC regression-followers-2: a blank, non-numeric or out-of-range owner-tuned value never breaks the import
    (main.py imports this module) or the guardian; int(float(v)) accepts '6.0'; the result is clamped to
    [lo, hi] and a value that differs from the raw setting is logged."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        logger.warning(f"{name}={raw!r} is not a number - using {default}")
        return default
    clamped = max(lo, min(hi, value))
    if clamped != value:
        logger.warning(f"{name}={raw!r} is outside [{lo}, {hi}] - using {clamped}")
    return clamped


# Configuration
GUARDIAN_INTERVAL_SECONDS = 120       # cycle cadence
EXCHANGE_SCAN_EVERY_N_CYCLES = 5      # legacy / rowless exchange scan every 5th cycle
INVARIANT_SCAN_EVERY_N_CYCLES = 2     # shadow dry pass / enforce full pass every 2nd cycle
CLOSE_MAX_RETRIES = 5
CLOSE_INITIAL_BACKOFF = 2.0           # 2s, 4s, 8s, 16s
MODES = ('legacy', 'shadow', 'enforce')
GUARDIAN_MAX_CONCURRENCY = _env_int('GUARDIAN_MAX_CONCURRENCY', 8, 1, 64)
LEGACY_FLAT_CONFIRM_S = 5.0           # legacy recovery: a flat finding needs a re-read this much later
ALARM_DEDUP_S = 3600.0                # error_logs + notify_admin dedup per (user, symbol, code)
PASS_SLOW_FRACTION = 0.8              # GUARDIAN_PASS_SLOW above 80% of the cadence
ALREADY_FLAT = 'already-flat'         # _attempt_market_close: ONE flat read, no order sent (never a close)
BLIND_AFTER = 3                       # I1: the 3rd failed read of a user in a row raises GUARDIAN_BLIND

# SQL (module constants so the offline tests can dispatch on them exactly)
SQL_BUSY = (
    "SELECT EXISTS (SELECT 1 FROM signal_deliveries WHERE user_id = $1 "
    "AND acknowledged_at > NOW() - INTERVAL '180 seconds' "
    "AND ($2::timestamp IS NULL OR acknowledged_at > $2))"
)
SQL_ROWS_FOR_USER = """
    SELECT id, user_id, signal_id, entry_order_id, tp_order_id, sl_order_id,
           symbol, kraken_symbol, side, quantity, leverage, entry_fill_price,
           target_tp, target_sl, opened_at, status
    FROM open_positions
    WHERE user_id = $1
      AND (status IN ('open', 'needs_recovery') OR opened_at > NOW() - INTERVAL '7 days')
    ORDER BY opened_at DESC
"""
SQL_USERS_WITH_ACTIVE_ROWS = """
    SELECT u.id, u.api_key, u.email, u.kraken_api_key_encrypted, u.kraken_api_secret_encrypted
    FROM follower_users u
    JOIN open_positions op ON op.user_id = u.id
    WHERE op.status IN ('open', 'needs_recovery')
      AND u.kraken_api_key_encrypted IS NOT NULL
    GROUP BY u.id, u.api_key, u.email, u.kraken_api_key_encrypted, u.kraken_api_secret_encrypted
    ORDER BY MIN(op.opened_at)
"""
SQL_ACTIVE_USERS = """
    SELECT id, api_key, email, kraken_api_key_encrypted, kraken_api_secret_encrypted
    FROM follower_users
    WHERE agent_active = true
    AND credentials_set = true
    AND kraken_api_key_encrypted IS NOT NULL
"""
SQL_RECENTLY_CLOSED_USERS = """
    SELECT DISTINCT u.id, u.api_key, u.email, u.kraken_api_key_encrypted, u.kraken_api_secret_encrypted
    FROM follower_users u
    JOIN open_positions op ON op.user_id = u.id
    WHERE op.status NOT IN ('open', 'needs_recovery')
      AND op.last_fill_at > NOW() - INTERVAL '60 minutes'
      AND u.kraken_api_key_encrypted IS NOT NULL
"""
SQL_NEEDS_RECOVERY_ROWS = """
    SELECT
        op.id, op.user_id, op.entry_order_id, op.tp_order_id, op.sl_order_id,
        op.symbol, op.kraken_symbol, op.side, op.quantity,
        op.entry_fill_price, op.target_tp, op.target_sl, op.opened_at, op.status,
        u.api_key, u.email,
        u.kraken_api_key_encrypted, u.kraken_api_secret_encrypted
    FROM open_positions op
    JOIN follower_users u ON op.user_id = u.id
    WHERE op.status = 'needs_recovery'
"""
SQL_TRACKED_OPEN_ROWS = """
    SELECT COUNT(*) FROM open_positions
    WHERE user_id = $1 AND status = 'open'
    AND tp_order_id IS NOT NULL AND sl_order_id IS NOT NULL
"""
SQL_OTHER_ACTIVE_ROWS = (
    "SELECT COUNT(*) FROM open_positions WHERE user_id = $1 AND kraken_symbol = $2 "
    "AND status IN ('open', 'needs_recovery') AND id <> $3"
)
SQL_SET_SL = "UPDATE open_positions SET sl_order_id = $1 WHERE id = $2 AND status IN ('open', 'needs_recovery')"
SQL_SET_TP = "UPDATE open_positions SET tp_order_id = $1 WHERE id = $2 AND status IN ('open', 'needs_recovery')"
SQL_RESTORE_OPEN = (
    "UPDATE open_positions SET status = 'open', quantity = $2, "
    "entry_fill_price = COALESCE($3, entry_fill_price) WHERE id = $1 AND status = 'needs_recovery'"
)
SQL_MARK_STATUS = (
    "UPDATE open_positions SET status = $1, last_fill_at = NOW() WHERE id = $2 AND status = 'needs_recovery'"
)

# Encryption
ENCRYPTION_KEY = os.getenv("CREDENTIALS_ENCRYPTION_KEY")
_cipher = Fernet(ENCRYPTION_KEY.encode()) if ENCRYPTION_KEY else None


def _decrypt_credentials(encrypted_key: str, encrypted_secret: str):
    """Decrypt Kraken API credentials"""
    if not _cipher:
        return None, None
    try:
        api_key = _cipher.decrypt(encrypted_key.encode()).decode()
        api_secret = _cipher.decrypt(encrypted_secret.encode()).decode()
        return api_key, api_secret
    except Exception:
        return None, None


async def _log_error_to_db(pool, api_key: str, error_type: str, error_message: str, context: Optional[Dict] = None):
    """Log error to error_logs table"""
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO error_logs (api_key, error_type, error_message, context)
                   VALUES ($1, $2, $3, $4)""",
                api_key[:20] + "..." if api_key and len(api_key) > 20 else api_key,
                error_type,
                error_message[:500] if error_message else None,
                json.dumps(context, default=str) if context else None
            )
    except Exception as e:
        logger.error(f"Failed to log error to DB: {e}")


def _pos_from_ccxt(p):
    """ccxt krakenfutures position -> bracket_guard.Pos (contracts only, never contractSize)."""
    if p is None:
        return None
    side = 1 if str(p.get('side') or '').lower() == 'long' else -1
    try:
        entry = float(p.get('entryPrice') or 0) or None
    except (TypeError, ValueError):
        entry = None
    ts = p.get('timestamp')
    return bg.Pos(side, abs(float(p.get('contracts') or 0)), entry, float(ts) / 1000.0 if ts else None)


def _fingerprint(p):
    """Identity of a live Kraken position: side, fillTime (ccxt 'timestamp') and entryPrice."""
    return (p.get('side'), p.get('timestamp'), p.get('entryPrice'))


def _busy_since(rows):
    """I12: for needs_recovery rows only deliveries acknowledged after the newest such row count."""
    ts = [r['opened_at'] for r in rows if r.get('status') == 'needs_recovery' and r.get('opened_at') is not None]
    return max(ts) if ts else None


def _action_ctx(a):
    ctx = dict(a.ctx or {})
    if a.level is not None:
        ctx['level'] = a.level
    if a.qty is not None:
        ctx['qty'] = a.qty
    if a.leg is not None:
        ctx['oid'] = a.leg.oid
    return ctx


class PositionGuardian:
    """
    Enforces the two-leg bracket on follower positions (see module docstring).
    """

    def __init__(self, db_pool, clock=time.time, sleep=None, max_concurrency=None):
        self.db_pool = db_pool
        self.active_exchanges: Dict[str, ccxt.krakenfutures] = {}
        self._markets_loaded = set()
        self._clock = clock
        self._sleep = sleep or asyncio.sleep
        workers = max(1, int(max_concurrency or GUARDIAN_MAX_CONCURRENCY))
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='guardian')
        self._sem = asyncio.Semaphore(workers)
        self._pass_lock = asyncio.Lock()
        self.cadence_s = float(GUARDIAN_INTERVAL_SECONDS)
        self._flat = follower_guard.FlatTracker()
        self._legacy_flat = {}                # needs_recovery row id -> first confirmed-flat epoch (legacy recovery)
        self._churn = {}                      # (user_id, symbol) -> bracket_guard.ChurnGuard
        self._churn_row = {}                  # (user_id, symbol) -> row id its ChurnGuard last served (CA-B7-4)
        self._retired = defaultdict(set)      # (user_id, symbol) -> oids this process placed or replaced
        self._alarm_last = {}                 # (user_id, symbol, code) -> last error_logs/notify time
        self._read_failures = {}              # user_id -> failed reads in a row (GUARDIAN_BLIND at BLIND_AFTER)
        self._ticker_cache = {}

    # ==================== Plumbing ====================

    async def _call(self, fn, *args, **kwargs):
        """Run one blocking ccxt call on the guardian's own executor (never the default one)."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, functools.partial(fn, *args, **kwargs))

    async def _busy(self, user_id, since=None) -> bool:
        """In-flight execute_trade (in-process token) or a delivery acknowledged within 180 s."""
        if follower_guard.is_inflight(user_id):
            return True
        try:
            async with self.db_pool.acquire() as conn:
                return bool(await conn.fetchval(SQL_BUSY, user_id, since))
        except Exception as e:
            logger.warning(f"Busy check failed for user {user_id}: {e} - treating as busy")
            return True

    async def _yield(self, user):
        await self._alarm(user, '*', 'YIELD_INFLIGHT', 'info')

    async def _alarm(self, user, symbol, code, severity='warning', ctx=None):
        """Log every time; error_logs 'GUARDIAN_<CODE>' + notify_admin (critical) once per hour per key."""
        ctx = dict(ctx or {})
        uid = (user or {}).get('id')
        api_key = (user or {}).get('api_key') or 'system'
        error_type = code if code.startswith(('GUARDIAN_', 'ALARM_')) else 'GUARDIAN_' + code
        level = {'critical': logging.CRITICAL, 'warning': logging.WARNING}.get(severity, logging.INFO)
        logger.log(level, f"{error_type} sev={severity} user={uid} symbol={symbol} "
                          f"ctx={json.dumps(ctx, default=str)[:300]}")
        key = (uid, symbol, error_type)
        now = self._clock()
        last = self._alarm_last.get(key)
        if last is not None and now - last < ALARM_DEDUP_S:
            return
        self._alarm_last[key] = now
        await _log_error_to_db(self.db_pool, api_key, error_type, f"{error_type} ({severity}) {symbol}",
                               dict(ctx, user_id=uid, symbol=symbol, severity=severity))
        if severity == 'critical':
            await notify_admin(
                title=f"🛡️ Guardian: {error_type}",
                details={'User': (user or {}).get('email') or api_key[:15], 'Symbol': symbol,
                         **{k: str(v)[:200] for k, v in ctx.items()}},
                level="error"
            )

    async def _read_failed(self, user, symbol, call, error):
        """
        I1: a failed read never acts. Every failed guardian read (load_markets, the invariant's
        orders/positions book read, its confirming re-reads and fills read) raises READ_FAILED
        (warning); the BLIND_AFTER-th failed read of one user in a row (no good book read in
        between) raises GUARDIAN_BLIND (critical: error_logs + notify_admin, hourly per user).
        """
        uid = (user or {}).get('id')
        n = self._read_failures[uid] = self._read_failures.get(uid, 0) + 1
        ctx = {'call': call, 'error': repr(error)[:200], 'consecutive': n}
        await self._alarm(user, symbol, 'READ_FAILED', 'warning', ctx)
        if n >= BLIND_AFTER:
            await self._alarm(user, '*', 'GUARDIAN_BLIND', 'critical', ctx)

    def _read_ok(self, user):
        """A good book read (orders + positions) ends the user's failed-read streak."""
        n = self._read_failures.pop(user['id'], 0)
        if n >= BLIND_AFTER:
            logger.warning(f"GUARDIAN_BLIND cleared user={user['id']} after {n} failed reads in a row")

    def _get_exchange(self, user_api_key: str, kraken_key: str, kraken_secret: str) -> Optional[ccxt.krakenfutures]:
        """Get or create cached exchange instance"""
        if user_api_key in self.active_exchanges:
            return self.active_exchanges[user_api_key]
        try:
            exchange = ccxt.krakenfutures({
                'apiKey': kraken_key,
                'secret': kraken_secret,
                'enableRateLimit': True,
            })
            self.active_exchanges[user_api_key] = exchange
            return exchange
        except Exception as e:
            logger.error(f"Failed to create exchange: {e}")
            return None

    async def _exchange_for(self, user) -> Optional[ccxt.krakenfutures]:
        """Cached exchange with markets loaded once (on the guardian pool)."""
        api_key = user['api_key']
        exchange = self.active_exchanges.get(api_key)
        if exchange is None:
            kraken_key, kraken_secret = _decrypt_credentials(
                user.get('kraken_api_key_encrypted'), user.get('kraken_api_secret_encrypted'))
            if not kraken_key:
                logger.error(f"❌ GUARDIAN: Cannot decrypt credentials for {api_key[:15]}...")
                return None
            exchange = self._get_exchange(api_key, kraken_key, kraken_secret)
            if exchange is None:
                return None
        if api_key not in self._markets_loaded:
            try:
                await self._call(exchange.load_markets)
            except Exception as e:
                await self._read_failed(user, '*', 'load_markets', e)
                return None
            self._markets_loaded.add(api_key)
        return exchange

    @staticmethod
    def _unified(exchange, symbol):
        try:
            return exchange.market(symbol)['symbol']
        except Exception:
            return symbol

    async def _symbol_position(self, exchange, symbol):
        """Symbol-matched no-arg fetch_positions (PF_ gotcha). Returns the live position or None; raises on error."""
        want = self._unified(exchange, symbol)
        positions = await self._call(exchange.fetch_positions)
        for p in positions or []:
            if p.get('symbol') == want and abs(float(p.get('contracts') or 0)) > 0:
                return p
        return None

    async def _rows_for_user(self, user_id):
        async with self.db_pool.acquire() as conn:
            return [dict(r) for r in await conn.fetch(SQL_ROWS_FOR_USER, user_id)]

    async def _cancel_single(self, exchange, order_id, symbol) -> bool:
        """One cancel_order call. OrderNotFound counts as done. Never a cancel-all."""
        try:
            await self._call(exchange.cancel_order, order_id, symbol)
            logger.info(f"   ✅ GUARDIAN: cancelled {order_id} on {symbol}")
            return True
        except ccxt.OrderNotFound:
            return True
        except Exception as e:
            logger.warning(f"   ⚠️ GUARDIAN: cancel {order_id} on {symbol} failed: {e}")
            return False

    async def _cancel_row_legs(self, exchange, row, symbol) -> bool:
        """Legacy edit (4): cancel the row's own real leg ids (only rows from the new failure paths carry ids).
        True when every real leg is gone (OrderNotFound counts as done). QC money-followers-3 (HL CA-B8-3 twin):
        callers keep a row needs_recovery when this is False, so a later cycle retries the cancel."""
        ok = True
        for key in ('sl_order_id', 'tp_order_id'):
            oid = follower_guard.real_oid(row.get(key))
            if oid:
                ok = await self._cancel_single(exchange, oid, symbol) and ok
        return ok

    async def _finish_recovery_row(self, exchange, user, row, symbol, status) -> bool:
        """Cancel a needs_recovery row's own legs, then mark it `status`. A failed cancel keeps the row
        needs_recovery (retried next cycle) and raises the critical GUARDIAN_RECOVERY_CANCEL_FAILED."""
        if not await self._cancel_row_legs(exchange, row, symbol):
            await self._alarm(user, symbol, 'RECOVERY_CANCEL_FAILED', 'critical',
                              {'row': row['id'], 'sl_order_id': row.get('sl_order_id'),
                               'tp_order_id': row.get('tp_order_id'), 'wanted_status': status})
            return False
        await self._mark_position_status(row['id'], status)
        return True

    async def _attempt_market_close(
        self,
        exchange: ccxt.krakenfutures,
        symbol: str,
        exit_side: str,
        quantity: float,
    ) -> Optional[str]:
        """
        Reduce-only market close with retry and a symbol-matched flat check before
        each attempt. Returns the close order id, ALREADY_FLAT, or None on failure.
        ALREADY_FLAT rests on ONE flat read: no caller treats it as a close or as a
        confirmed flat (no leg cancel and no status change on it alone).
        """
        for attempt in range(1, CLOSE_MAX_RETRIES + 1):
            try:
                if await self._symbol_position(exchange, symbol) is None:
                    logger.info(f"   ✅ {symbol} is flat - no close order sent")
                    return ALREADY_FLAT
            except Exception as e:
                logger.warning(f"   ⚠️ Flat check before close attempt {attempt} failed: {e}")
            try:
                order = await self._call(
                    exchange.create_order,
                    symbol=symbol,
                    type='market',
                    side=exit_side,
                    amount=quantity,
                    params={'reduceOnly': True}
                )
                order_id = str(order.get('id') or 'unknown-close-id')
                logger.info(f"   ✅ Market close successful (attempt {attempt}): {order_id}")
                return order_id
            except Exception as e:
                logger.warning(f"   ❌ Market close attempt {attempt}/{CLOSE_MAX_RETRIES} failed: {e}")
                if attempt < CLOSE_MAX_RETRIES:
                    backoff = CLOSE_INITIAL_BACKOFF * (2 ** (attempt - 1))
                    await self._sleep(backoff)

        logger.error(f"   ❌ All {CLOSE_MAX_RETRIES} close attempts failed for {symbol}")
        return None

    async def _mark_position_status(self, position_id: int, status: str):
        """needs_recovery -> closed / closed_manual only (legacy edit 5: status guard)."""
        try:
            async with self.db_pool.acquire() as conn:
                await conn.execute(SQL_MARK_STATUS, status, position_id)
        except Exception as e:
            logger.error(f"Failed to update position {position_id} status: {e}")

    async def _restore_open(self, row, pos):
        """I17: needs_recovery -> 'open' after a full restore, with the live qty and entry price."""
        async with self.db_pool.acquire() as conn:
            await conn.execute(SQL_RESTORE_OPEN, row['id'], pos.qty, pos.entry_px)
        logger.info(f"✅ GUARDIAN: needs_recovery row {row['id']} restored to 'open' (bracket complete)")

    # ==================== Startup Recovery (legacy / shadow) ====================

    async def startup_recovery(self):
        """
        Run once on boot. For each active user, check if they have open positions
        on Kraken without matching TP/SL orders. Close any unprotected positions.
        """
        logger.info("🔍 Running startup recovery scan...")

        try:
            async with self.db_pool.acquire() as conn:
                users = await conn.fetch(SQL_ACTIVE_USERS)

            recovered = 0
            for user in users:
                try:
                    result = await self._check_user_for_unprotected(dict(user))
                    if result:
                        recovered += result
                except Exception as e:
                    logger.warning(f"Startup scan error for user {user['api_key'][:15]}...: {e}")

            if recovered > 0:
                logger.info(f"🚨 Startup recovery: closed {recovered} unprotected position(s)")
                await notify_admin(
                    title="🛡️ Guardian Startup Recovery",
                    details={
                        "Positions Closed": recovered,
                        "Trigger": "Service restart/deploy",
                        "Action": "Unprotected positions closed at market",
                    },
                    level="warning"
                )
            else:
                logger.info("✅ Startup recovery: all positions protected")

        except Exception as e:
            logger.error(f"❌ Startup recovery failed: {e}")
            await _log_error_to_db(
                self.db_pool, "system", "GUARDIAN_STARTUP_ERROR",
                str(e), {"function": "startup_recovery"}
            )

    async def _check_user_for_unprotected(self, user: dict, rowless_only: bool = False) -> int:
        """
        Today's rule (follower_guard.legacy_rowless_should_close) for one user.
        rowless_only=True (enforce): only (user, symbol) pairs without an active row.
        Returns number of positions closed.
        """
        if await self._busy(user['id']):                                   # legacy edit (1)
            return 0
        exchange = await self._exchange_for(user)
        if not exchange:
            return 0

        user_short = user['api_key'][:15] + "..."

        try:
            positions = await self._call(exchange.fetch_positions)
        except Exception as e:
            logger.debug(f"Could not fetch positions for {user_short}: {e}")
            return 0

        # Find positions with non-zero contracts
        open_positions = []
        for pos in positions:
            contracts = abs(float(pos.get('contracts') or pos.get('contractSize') or 0))
            if contracts > 0:
                open_positions.append(pos)

        if not open_positions:
            return 0

        # Fetch open orders
        try:
            open_orders = await self._call(exchange.fetch_open_orders)
        except Exception as e:
            logger.warning(f"Could not fetch orders for {user_short}: {e}")
            return 0

        rowed = set()
        if rowless_only:
            rowed = {self._unified(exchange, r.get('kraken_symbol') or r.get('symbol'))
                     for r in await self._rows_for_user(user['id'])
                     if r['status'] in follower_guard.ACTIVE_STATUSES}

        closed_count = 0
        for pos in open_positions:
            symbol = pos.get('symbol')
            if symbol in rowed:
                continue                                   # the invariant owns rowed symbols
            contracts = abs(float(pos.get('contracts') or pos.get('contractSize') or 0))
            pos_side = pos.get('side', '').lower()  # 'long' or 'short'

            # Check if there are protective orders for this symbol
            symbol_orders = [o for o in open_orders if o.get('symbol') == symbol]

            # Also check: does the DB know about this position?
            async with self.db_pool.acquire() as conn:
                db_position = await conn.fetchval(SQL_TRACKED_OPEN_ROWS, user['id'])

            if not follower_guard.legacy_rowless_should_close(symbol_orders, db_position):
                # Position has some protection or is tracked — skip
                continue

            if await self._busy(user['id']):
                return closed_count

            # UNPROTECTED POSITION FOUND
            exit_side = 'sell' if pos_side == 'long' else 'buy'
            logger.critical(
                f"🚨 GUARDIAN: Unprotected position found for {user_short}: "
                f"{symbol} {pos_side} {contracts} contracts - closing at market"
            )

            order_id = await self._attempt_market_close(exchange, symbol, exit_side, contracts)

            if order_id == ALREADY_FLAT:
                continue
            if order_id:
                closed_count += 1
                logger.info(f"✅ GUARDIAN: Closed unprotected position for {user_short}")

                await notify_admin(
                    title="🛡️ Guardian: Unprotected Position Closed",
                    details={
                        "User": user_short,
                        "Symbol": symbol,
                        "Side": pos_side,
                        "Contracts": contracts,
                        "Close Order": order_id,
                        "Trigger": "Rowless exchange scan" if rowless_only else "Startup recovery scan",
                    },
                    level="warning"
                )
            else:
                logger.error(f"❌ GUARDIAN: Failed to close unprotected position for {user_short}")
                await _log_error_to_db(
                    self.db_pool, user['api_key'], "GUARDIAN_CLOSE_FAILED",
                    f"Failed to close unprotected {symbol} {pos_side} {contracts}",
                    {"function": "check_user_for_unprotected"}
                )

        return closed_count

    # ==================== DB-Driven Recovery (legacy / shadow) ====================

    async def recover_db_positions(self):
        """
        Check for positions with status='needs_recovery' and attempt to close them.
        """
        try:
            async with self.db_pool.acquire() as conn:
                positions = await conn.fetch(SQL_NEEDS_RECOVERY_ROWS)

            if not positions:
                self._legacy_flat.clear()
                return

            logger.info(f"🔧 Found {len(positions)} position(s) needing recovery")

            ids = {p['id'] for p in positions}
            for row_id in [k for k in self._legacy_flat if k not in ids]:
                del self._legacy_flat[row_id]
            for pos in positions:
                pos = dict(pos)
                await self._recover_single_position(pos)

        except Exception as e:
            logger.error(f"❌ DB recovery scan failed: {e}")
            await _log_error_to_db(
                self.db_pool, "system", "GUARDIAN_DB_RECOVERY_ERROR",
                str(e), {"function": "recover_db_positions"}
            )

    async def _row_bracket_intact(self, exchange, user, row, symbol, live):
        """
        A7: before a legacy close, read the orders. True when a valid SL AND a valid TP
        rest at the row's recorded levels (bracket_guard.decide finds nothing to place),
        False otherwise, None when the orders read failed (no action this cycle).
        CA-B7-3: a failed orders read goes through _read_failed (READ_FAILED, and the 3rd
        failed read of the user in a row raises GUARDIAN_BLIND); a good one completes a good
        book read (the caller has just read the live position), which ends the streak.
        """
        rec = follower_guard.rec_from_row(row, 'last', bg.TP_LIMIT)
        pos = _pos_from_ccxt(live)
        if rec is None or pos is None or rec.side != pos.side:
            return False
        try:
            orders = await self._call(exchange.fetch_open_orders)
        except Exception as e:
            logger.warning(f"⚠️ GUARDIAN: order read before legacy close failed for row {row['id']}: {e}")
            await self._read_failed(user, symbol, 'fetch_open_orders', e)
            return None
        self._read_ok(user)
        try:
            market = exchange.market(self._unified(exchange, symbol))
        except Exception as e:                  # a local market lookup, not an exchange read: plain READ_FAILED
            await self._alarm(user, symbol, 'READ_FAILED', 'warning', {'call': 'market', 'error': repr(e)[:200]})
            return None
        legs = [bg.leg_from_kraken(o) for o in orders or []
                if bg.kraken_order_matches(o, market['symbol'], market['id'])]
        acts = bg.decide(pos, rec, legs, {}, float(market['precision']['price']),
                         float(market['precision']['amount']), self._clock(),
                         level_policy=follower_guard.DECIDE_LEVEL_POLICY)
        return not any(a.kind in ('PLACE_SL', 'PLACE_TP', 'CLOSE') for a in acts)

    async def _legacy_flat_persisted(self, row) -> bool:
        """
        QC correctness-followers-2: a legacy confirmed flat (two reads 5 s apart) cancels the row's legs and
        marks it closed_manual only once the flat has persisted bg.FLAT_PERSIST_S (240 s) across cycles, the
        rule the invariant applies to needs_recovery rows. Two false-empty reads never strip a live SL.
        """
        now = self._clock()
        first = self._legacy_flat.setdefault(row['id'], now)
        if now - first < bg.FLAT_PERSIST_S:
            logger.info(f"   GUARDIAN: row {row['id']} confirmed flat for {int(now - first)}s - legs kept until "
                        f"{bg.FLAT_PERSIST_S}s")
            return False
        return True

    async def _recover_single_position(self, pos: dict):
        """Legacy close of one needs_recovery row, with the Wave 1 safety edits (1)-(6) and A7."""
        user_short = pos['api_key'][:15] + "..."
        kraken_symbol = pos['kraken_symbol'] or pos['symbol']
        side = (pos.get('side') or '').upper()
        quantity = float(pos.get('quantity') or 0)
        user = {'id': pos['user_id'], 'api_key': pos['api_key'], 'email': pos.get('email'),
                'kraken_api_key_encrypted': pos.get('kraken_api_key_encrypted'),
                'kraken_api_secret_encrypted': pos.get('kraken_api_secret_encrypted')}

        if await self._busy(pos['user_id'], since=pos.get('opened_at')):   # edit (1)
            return

        async with self.db_pool.acquire() as conn:                          # edit (6)
            others = await conn.fetchval(SQL_OTHER_ACTIVE_ROWS, pos['user_id'], kraken_symbol, pos['id'])
        if others:
            await self._alarm(user, kraken_symbol, 'ROW_AMBIGUOUS', 'critical', {'row': pos['id'], 'other_active': others})
            return

        if not quantity:
            logger.warning(f"⚠️ GUARDIAN: Position {pos['id']} has zero quantity, marking closed")
            await self._mark_position_status(pos['id'], 'closed_manual')
            return

        exchange = await self._exchange_for(user)
        if not exchange:
            return

        # Check if the row's symbol still has a position on Kraken (edits 2 + 3)
        try:
            live = await self._symbol_position(exchange, kraken_symbol)
            if live is None:
                await self._sleep(LEGACY_FLAT_CONFIRM_S)
                live = await self._symbol_position(exchange, kraken_symbol)
        except Exception as e:
            logger.warning(f"⚠️ GUARDIAN: Could not check Kraken positions for {user_short}: {e}")
            # Don't close blindly — skip this cycle and try again next time
            return

        if live is None:
            # Position already closed (user closed manually or liquidated)
            if not await self._legacy_flat_persisted(pos):
                return
            logger.info(f"✅ GUARDIAN: Position {pos['id']} no longer exists on Kraken - marking closed")
            if await self._finish_recovery_row(exchange, user, pos, kraken_symbol, 'closed_manual'):   # edit (4)
                self._legacy_flat.pop(pos['id'], None)
            return
        self._legacy_flat.pop(pos['id'], None)                              # seen live: the flat clock restarts

        intact = await self._row_bracket_intact(exchange, user, pos, kraken_symbol, live)   # A7
        if intact is None:
            return
        if intact:
            await self._restore_open(pos, _pos_from_ccxt(live))
            await self._alarm(user, kraken_symbol, 'RECOVERY_RESTORED', 'warning', {'row': pos['id']})
            return

        if await self._busy(pos['user_id'], since=pos.get('opened_at')):
            return

        # Position still open — close it
        exit_side = 'sell' if side in ('BUY', 'LONG') else 'buy'

        logger.info(f"🔧 GUARDIAN: Closing needs_recovery position {pos['id']} for {user_short}: {kraken_symbol} {side} {quantity}")

        order_id = await self._attempt_market_close(exchange, kraken_symbol, exit_side, quantity)

        if order_id == ALREADY_FLAT:
            # The close helper read the symbol flat ONCE: neither a close nor a confirmed flat (edits 3 + 4).
            # Re-read 5 s later; only a confirmed flat cancels the row's legs and marks it closed_manual.
            await self._sleep(LEGACY_FLAT_CONFIRM_S)
            try:
                live = await self._symbol_position(exchange, kraken_symbol)
            except Exception as e:
                logger.warning(f"⚠️ GUARDIAN: Could not re-check Kraken positions for {user_short}: {e}")
                return
            if live is not None:
                logger.warning(f"⚠️ GUARDIAN: {kraken_symbol} read flat once but is still open - "
                               f"position {pos['id']} stays needs_recovery for the next cycle")
                return
            if not await self._legacy_flat_persisted(pos):
                return
            logger.info(f"✅ GUARDIAN: Position {pos['id']} confirmed flat for {bg.FLAT_PERSIST_S}s - marking closed_manual")
            if await self._finish_recovery_row(exchange, user, pos, kraken_symbol, 'closed_manual'):   # edit (4)
                self._legacy_flat.pop(pos['id'], None)
            return

        if order_id:
            await self._finish_recovery_row(exchange, user, pos, kraken_symbol, 'closed')   # edit (4)
            logger.info(f"✅ GUARDIAN: Position {pos['id']} closed successfully: {order_id}")
            await self._alarm(user, kraken_symbol, 'RECOVERY_CLOSE', 'warning',      # CA-B7-5: error_logs row
                              {'position_id': pos['id'], 'order': order_id, 'side': side, 'quantity': quantity})

            await notify_admin(                                                     # CA-B7-5: every close notifies
                title="🛡️ Guardian: GUARDIAN_RECOVERY_CLOSE (recovery close successful)",
                details={
                    "User": pos.get('email', user_short),
                    "Position ID": pos['id'],
                    "Symbol": kraken_symbol,
                    "Side": side,
                    "Quantity": quantity,
                    "Close Order": order_id,
                    "Original Entry": pos.get('entry_order_id', 'unknown'),
                    "Time Unprotected": str(datetime.utcnow() - pos['opened_at']) if pos.get('opened_at') else 'unknown',
                },
                level="warning"
            )
        else:
            logger.error(f"❌ GUARDIAN: Failed to close position {pos['id']} - will retry next cycle")
            await self._alarm(user, kraken_symbol, 'RECOVERY_CLOSE_FAILED', 'critical',
                              {'position_id': pos['id'], 'side': side, 'quantity': quantity})

    # ==================== Exchange Scan (Catch-All) ====================

    async def scan_exchange_for_unprotected(self, rowless_only: bool = False):
        """
        Exchange scan with today's rule. rowless_only=True (enforce) skips symbols with an active row.
        """
        logger.info("🔍 Running full exchange scan for unprotected positions...")

        try:
            async with self.db_pool.acquire() as conn:
                users = await conn.fetch(SQL_ACTIVE_USERS)

            issues_found = 0
            for user in users:
                try:
                    result = await self._check_user_for_unprotected(dict(user), rowless_only=rowless_only)
                    if result:
                        issues_found += result
                except Exception as e:
                    logger.debug(f"Exchange scan error for user: {e}")

            if issues_found > 0:
                logger.warning(f"🚨 Exchange scan: closed {issues_found} unprotected position(s)")
            else:
                logger.debug("✅ Exchange scan: all positions protected")

        except Exception as e:
            logger.error(f"❌ Exchange scan failed: {e}")

    # ==================== Two-leg invariant (shadow = dry, enforce = acts) ====================

    async def _pass_users(self, full: bool):
        """Users with an active row first (oldest row first); full adds active and recently-closed users."""
        async with self.db_pool.acquire() as conn:
            first = [dict(r) for r in await conn.fetch(SQL_USERS_WITH_ACTIVE_ROWS)]
            if not full:
                return first
            rest = ([dict(r) for r in await conn.fetch(SQL_ACTIVE_USERS)]
                    + [dict(r) for r in await conn.fetch(SQL_RECENTLY_CLOSED_USERS)])
        seen = {u['id'] for u in first}
        users = list(first)
        for u in rest:
            if u['id'] not in seen:
                seen.add(u['id'])
                users.append(u)
        return users

    async def invariant_pass(self, full: bool, dry: bool) -> float:
        """One pass over the users; passes never overlap. Returns the pass duration in seconds."""
        async with self._pass_lock:
            t0 = time.monotonic()
            self._ticker_cache = {}
            users = await self._pass_users(full)
            active_ids = set()

            async def one(user):
                async with self._sem:
                    try:
                        await self._enforce_user(user, dry, active_ids)
                    except Exception as e:
                        await self._alarm(user, '*', 'EXCEPTION', 'critical', {'error': repr(e)[:200]})

            await asyncio.gather(*(one(u) for u in users))
            self._flat.gc(active_ids)
            self._gc_churn(active_ids)
            elapsed = time.monotonic() - t0
            if elapsed > self.cadence_s:
                await self._alarm(None, '*', 'BUDGET', 'critical', {'elapsed_s': round(elapsed, 2),
                                  'users': len(users), 'cadence_s': self.cadence_s})
            elif elapsed > PASS_SLOW_FRACTION * self.cadence_s:
                await self._alarm(None, '*', 'PASS_SLOW', 'warning', {'elapsed_s': round(elapsed, 2),
                                  'users': len(users), 'cadence_s': self.cadence_s})
            logger.info(f"GUARDIAN_PASS full={full} dry={dry} users={len(users)} elapsed={elapsed:.2f}s")
            return elapsed

    def _gc_churn(self, active_ids):
        """
        CA-B7-4 (runs with FlatTracker.gc at the end of every pass): drop the ChurnGuard state of each
        (user, symbol) pair whose row is no longer active (closed, or a rowless pair), so repair history and
        TP backoff never outlive the position they were counted for. A busy user's active row keeps its state:
        active_ids is filled before the busy check.
        """
        for key in list(self._churn):
            if self._churn_row.get(key) not in active_ids:
                del self._churn[key]
        for key in [k for k in self._churn_row if k not in self._churn]:
            del self._churn_row[key]

    async def _enforce_user(self, user, dry, active_ids):
        uid = user['id']
        rows = await self._rows_for_user(uid)
        active_ids.update(r['id'] for r in rows if r['status'] in follower_guard.ACTIVE_STATUSES)
        since = _busy_since(rows)
        if await self._busy(uid, since):
            return await self._yield(user)
        exchange = await self._exchange_for(user)
        if exchange is None:
            return
        call = 'fetch_open_orders'
        try:
            orders = await self._call(exchange.fetch_open_orders)      # FIRST (no-arg: includes trigger orders)
            call = 'fetch_positions'
            positions = await self._call(exchange.fetch_positions)     # SECOND (no-arg: PF_ gotcha)
        except Exception as e:
            return await self._read_failed(user, '*', call, e)         # never acts; the 3rd in a row: GUARDIAN_BLIND
        self._read_ok(user)
        if await self._busy(uid, since):
            return await self._yield(user)
        by_sym = defaultdict(list)
        for r in rows:                                                  # LISTS, never one row per symbol
            by_sym[self._unified(exchange, r.get('kraken_symbol') or r.get('symbol'))].append(r)
        live = {p.get('symbol'): p for p in positions or [] if abs(float(p.get('contracts') or 0)) > 0}
        syms = (set(live)
                | {s for s, rs in by_sym.items() if any(r['status'] in follower_guard.ACTIVE_STATUSES for r in rs)}
                | {o.get('symbol') for o in orders or [] if o.get('symbol')})
        for sym in sorted(s for s in syms if s):
            if not await self._enforce_symbol(exchange, user, sym, by_sym.get(sym, []), orders or [],
                                              live.get(sym), dry, since):
                return

    async def _enforce_symbol(self, exchange, user, sym, srows, orders, p, dry, since) -> bool:
        """Evaluate one (user, symbol). Returns False when the user became busy (yield the rest of the pass)."""
        uid = user['id']
        try:
            market = exchange.market(sym)
            tick, lot = float(market['precision']['price']), float(market['precision']['amount'])
        except Exception as e:
            await self._alarm(user, sym, 'READ_FAILED', 'warning', {'call': 'market', 'error': repr(e)[:200]})
            return True
        active = [r for r in srows if r['status'] in follower_guard.ACTIVE_STATUSES]
        pos = _pos_from_ccxt(p)
        row, code = follower_guard.select_row(active, pos, lot, self._flat.evidence)
        if row is not None and pos is not None and self._flat.pending_check(row['id']):
            if not await self._resolve_reappearance(exchange, user, sym, row, p):
                return True
            row, code = follower_guard.select_row(active, pos, lot, self._flat.evidence)
        if code:
            await self._alarm(user, sym, code, 'critical', {'rows': [r['id'] for r in active]})
            return True
        if row is None and pos is not None:
            return True                        # rowless live position: the 5th-cycle legacy scan owns it
        if row is not None and pos is not None:
            self._flat.note_live(row['id'], _fingerprint(p))
        if row is not None and row['status'] == 'open' and pos is None:
            await self._note_open_row_flat(exchange, user, sym, row)
            return True                        # position_monitor records the trade, then cancels the leftover
        legs = [bg.leg_from_kraken(o) for o in orders if bg.kraken_order_matches(o, market['symbol'], market['id'])]
        owned = follower_guard.service_oids(srows) | self._retired[(uid, sym)]
        if row is None and not any(leg.oid in owned for leg in legs):
            return True                        # flat, rowless, only foreign orders: nothing to do
        rec = follower_guard.rec_from_row(row, 'last', bg.TP_LIMIT) if row is not None else None
        if row is not None and rec is None:
            return await self._invalid_levels(exchange, user, sym, row, dry)
        now = self._clock()
        ref = {'last': await self._ticker_last(exchange, sym)} if pos is not None else {}
        busy = await self._busy(uid, since)
        acts = bg.decide(pos, rec, legs, ref, tick, lot, now, in_flight=busy,
                         level_policy=follower_guard.DECIDE_LEVEL_POLICY)
        if busy:
            await self._yield(user)
            return False
        if any(a.kind == 'CONFIRM_FLAT' for a in acts):
            await self._sleep(bg.FLAT_CONFIRM_S)
            try:
                orders = await self._call(exchange.fetch_open_orders)     # re-read: orders FIRST
                p = await self._symbol_position(exchange, sym)           # positions SECOND
            except Exception as e:
                await self._read_failed(user, sym, 'confirm_flat_reread', e)
                return True
            if p is not None:
                return True                    # flat not confirmed; the next pass evaluates the position
            legs = [bg.leg_from_kraken(o) for o in orders or []
                    if bg.kraken_order_matches(o, market['symbol'], market['id'])]
            now = self._clock()
            flat_since = None
            if row is not None:
                age = self._flat.note_confirmed_flat(row['id'], now)
                flat_since = now - age
                if row['status'] == 'needs_recovery' and age < bg.FLAT_PERSIST_S:
                    return True                # needs_recovery -> closed_manual only after 240 s confirmed flat
            busy = await self._busy(uid, since)
            acts = bg.decide(None, rec, legs, {}, tick, lot, now, flat_confirmed=True, flat_since=flat_since,
                             in_flight=busy, level_policy=follower_guard.DECIDE_LEVEL_POLICY)
            if busy:
                await self._yield(user)
                return False
        raw = list(acts)
        acts, alarms = follower_guard.filter_actions(acts, row, owned, pos=pos, ref_px=ref.get('last'))
        rid = row['id'] if row is not None else None
        if self._churn_row.get((uid, sym), rid) != rid:
            self._churn.pop((uid, sym), None)          # CA-B7-4: churn state is per row, never inherited
        churn = self._churn.setdefault((uid, sym), bg.ChurnGuard())
        self._churn_row[(uid, sym)] = rid              # CA-B7-4: binding for the pass-end GC
        acts, more = churn.filter(acts, legs, now, qty=pos.qty if pos is not None else None, rec=rec)
        for alarm_code, severity, ctx in alarms + more:
            await self._alarm(user, sym, alarm_code, severity, ctx)
        return await self._execute(exchange, user, sym, row, pos, rec, raw, acts, churn, dry, since)

    async def _resolve_reappearance(self, exchange, user, sym, row, p) -> bool:
        """
        R3-M6 + A2: a position reappeared for a row with a confirmed flat. Same fillTime
        fingerprint -> false flat. Otherwise fills decide: an entry-side fill since
        FlatTracker.since_ms(row) (clamped to the row's opened_at), other than the row's own
        entry order, is new-entry evidence (ROW_AMBIGUOUS from then on). False = fills read failed.
        """
        fp = _fingerprint(p)
        if fp == self._flat.fp.get(row['id']):
            self._flat.resolve(row['id'], False, fp)
            await self._alarm(user, sym, 'FALSE_FLAT_CLEARED', 'warning', {'row': row['id']})
            return True
        since_ms = self._flat.since_ms(row)
        try:
            trades = await self._call(exchange.fetch_my_trades, sym, since_ms)
        except Exception as e:
            await self._read_failed(user, sym, 'fetch_my_trades', e)
            return False
        fills = [{'side': t.get('side'), 'ts_ms': t.get('timestamp'), 'opening': None,
                  'order_id': str(t['order']) if t.get('order') is not None else None} for t in trades or []]
        new = follower_guard.fills_show_new_entry(fills, follower_guard.row_side(row), since_ms,
                                                  row.get('entry_order_id'))
        self._flat.resolve(row['id'], new, None if new else fp)
        if not new:
            await self._alarm(user, sym, 'FALSE_FLAT_CLEARED', 'warning', {'row': row['id']})
        return True

    async def _note_open_row_flat(self, exchange, user, sym, row):
        """'open' row, symbol flat: re-read both after 5 s; no decide(), no cancel, no UPDATE."""
        await self._sleep(bg.FLAT_CONFIRM_S)
        try:
            await self._call(exchange.fetch_open_orders)                 # re-read: orders FIRST
            p = await self._symbol_position(exchange, sym)               # positions SECOND
        except Exception as e:
            await self._read_failed(user, sym, 'open_row_flat_reread', e)
            return
        if p is not None:
            return                             # a single empty read never counts as flat
        age = self._flat.note_confirmed_flat(row['id'], self._clock())
        if age > follower_guard.FLAT_ROW_UNRECORDED_S:
            await self._alarm(user, sym, 'FLAT_ROW_UNRECORDED', 'warning', {'row': row['id'], 'flat_s': int(age)})

    async def _invalid_levels(self, exchange, user, sym, row, dry) -> bool:
        if row['status'] != 'needs_recovery':
            await self._alarm(user, sym, 'ROW_LEVELS_INVALID', 'critical', {'row': row['id']})
            return True
        if dry:
            await self._alarm(user, sym, 'WOULD_LEGACY_CLOSE', 'info', {'row': row['id']})
            return True
        await self._recover_single_position(dict(
            row, api_key=user['api_key'], email=user.get('email'),
            kraken_api_key_encrypted=user.get('kraken_api_key_encrypted'),
            kraken_api_secret_encrypted=user.get('kraken_api_secret_encrypted')))
        return True

    async def _ticker_last(self, exchange, sym):
        if sym in self._ticker_cache:
            return self._ticker_cache[sym]
        try:
            ticker = await self._call(exchange.fetch_ticker, sym)
            px = float(ticker['last']) if ticker and ticker.get('last') is not None else None
        except Exception as e:
            logger.warning(f"⚠️ GUARDIAN: ticker read failed for {sym}: {e}")
            px = None
        self._ticker_cache[sym] = px
        return px

    async def _place_leg(self, exchange, sym, leg, pos, level) -> Optional[str]:
        """PLACE_SL: stop (stop-market) reduce-only; PLACE_TP: limit reduce-only. 3 attempts."""
        xs = 'sell' if pos.side > 0 else 'buy'
        for attempt in range(1, bg.SL_PLACE_ATTEMPTS + 1):
            try:
                if leg == 'SL':
                    order = await self._call(exchange.create_order, sym, 'stop', xs, pos.qty, None,
                                             {'triggerPrice': level, 'reduceOnly': True})
                else:
                    order = await self._call(exchange.create_order, sym, 'limit', xs, pos.qty, level,
                                             {'reduceOnly': True})
                return str(order['id'])
            except Exception as e:
                logger.warning(f"   ❌ GUARDIAN: {leg} placement attempt {attempt}/{bg.SL_PLACE_ATTEMPTS} "
                               f"failed for {sym}: {e}")
                if attempt < bg.SL_PLACE_ATTEMPTS:
                    await self._sleep(attempt)
        return None

    async def _set_row_leg(self, row, sql, oid):
        if row is None:
            return
        async with self.db_pool.acquire() as conn:
            await conn.execute(sql, oid, row['id'])

    async def _close(self, exchange, user, sym, row, pos, code, severity='critical') -> bool:
        """Reduce-only market close. True only when a close order went out."""
        xs = 'sell' if pos.side > 0 else 'buy'
        order_id = await self._attempt_market_close(exchange, sym, xs, pos.qty)
        if not order_id:
            await self._alarm(user, sym, 'CLOSE_FAILED', 'critical', {'code': code})
            return False
        if order_id == ALREADY_FLAT:
            # ONE flat read is neither a close nor a confirmed flat (I5): no leg cancel, no row change. The next
            # pass re-reads: a live position is decided again; a real flat goes through CONFIRM_FLAT and the
            # FlatTracker (a needs_recovery row becomes closed_manual only after 240 s of confirmed flat).
            await self._alarm(user, sym, 'CLOSE_SKIPPED_FLAT', 'warning', {'code': code})
            return False
        self._churn.pop((user['id'], sym), None)
        await self._alarm(user, sym, 'CLOSE', severity, {'code': code, 'order': order_id})
        if row is not None and row['status'] == 'needs_recovery':
            await self._finish_recovery_row(exchange, user, row, sym, 'closed')
        return True                            # an 'open' row is left for position_monitor to record

    async def _execute(self, exchange, user, sym, row, pos, rec, raw, acts, churn, dry, since) -> bool:
        """Make-before-break executor; _busy() re-checked before every mutation."""
        uid = user['id']
        now = self._clock()
        protected = {o for o in (bg.trusted_sl_oid(rec), churn.placed['SL'][-1][1] if churn.placed['SL'] else None) if o}
        plan_places_sl = any(a.kind == 'PLACE_SL' for a in acts)
        placed_sl_ok = False
        cancels_ok = True                      # QC money-followers-3: MARK_ROW_CLOSED_MANUAL needs every cancel
        done = set()
        for a in acts:
            if a.kind == 'ALARM':
                await self._alarm(user, sym, a.code, a.severity, _action_ctx(a))
                continue
            if dry:
                await self._alarm(user, sym, 'WOULD_' + a.kind, 'info', dict(_action_ctx(a), code=a.code))
                continue
            if await self._busy(uid, since):
                await self._yield(user)
                return False
            if a.kind in ('PLACE_SL', 'PLACE_TP'):
                try:
                    if await self._symbol_position(exchange, sym) is None:
                        logger.info(f"   GUARDIAN: {sym} is flat now - repair skipped")
                        return True
                except Exception as e:
                    logger.warning(f"   ⚠️ GUARDIAN: position re-check failed ({e}) - placing anyway")
            if a.kind == 'PLACE_SL':
                oid = await self._place_leg(exchange, sym, 'SL', pos, a.level)
                if oid is None:
                    await self._alarm(user, sym, 'SL_REPAIR_FAILED_CLOSED', 'critical', {'level': a.level})
                    await self._close(exchange, user, sym, row, pos, 'SL_REPAIR_FAILED_CLOSED')
                    return True
                await self._set_row_leg(row, SQL_SET_SL, oid)
                self._retired[(uid, sym)].update(
                    o for o in (follower_guard.real_oid((row or {}).get('sl_order_id')), oid) if o)
                churn.note_placed('SL', oid, now)
                placed_sl_ok = True
                done.add('PLACE_SL')
                await self._alarm(user, sym, a.code or 'SL_REPAIRED', a.severity, {'level': a.level, 'oid': oid})
            elif a.kind == 'PLACE_TP':
                oid = await self._place_leg(exchange, sym, 'TP', pos, a.level)
                if oid is None:
                    churn.note_place_failed('TP', now)
                    await self._alarm(user, sym, 'TP_PLACE_FAILED', 'warning', {'level': a.level})
                    continue
                await self._set_row_leg(row, SQL_SET_TP, oid)
                self._retired[(uid, sym)].update(
                    o for o in (follower_guard.real_oid((row or {}).get('tp_order_id')), oid) if o)
                churn.note_placed('TP', oid, now)
                done.add('PLACE_TP')
                await self._alarm(user, sym, a.code or 'TP_REPAIRED', 'warning', {'level': a.level, 'oid': oid})
            elif a.kind == 'CANCEL':
                oid = follower_guard.real_oid(a.leg.oid if a.leg is not None else None)
                if oid in protected and a.code != 'ORPHAN_LEG' and not placed_sl_ok:
                    await self._alarm(user, sym, 'CLASSIFIER_SUSPECT', 'critical', {'leg': 'SL', 'oid': oid, 'dropped': a.code})
                    continue
                if a.code == 'SL_EXTRA_OR_INVALID' and plan_places_sl and not placed_sl_ok:
                    continue                   # make-before-break: the old SL stays until a new one is live
                cancels_ok = await self._cancel_single(exchange, oid, sym) and cancels_ok
            elif a.kind == 'CLOSE':
                await self._close(exchange, user, sym, row, pos, a.code, a.severity)
                return True
            elif a.kind == 'MARK_ROW_CLOSED_MANUAL':
                if not cancels_ok:             # a leg is still resting: keep the row; the next pass retries
                    await self._alarm(user, sym, 'RECOVERY_CANCEL_FAILED', 'critical',
                                      {'row': row['id'], 'wanted_status': 'closed_manual'})
                    continue
                await self._mark_position_status(row['id'], 'closed_manual')
                await self._alarm(user, sym, 'ROW_CLOSED_MANUAL', 'warning', {'row': row['id']})
        if (not dry and row is not None and row['status'] == 'needs_recovery' and pos is not None
                and not any(a.kind == 'CLOSE' for a in raw)
                and {a.kind for a in raw if a.kind in ('PLACE_SL', 'PLACE_TP')} <= done):
            await self._restore_open(row, pos)
            await self._alarm(user, sym, 'RECOVERY_RESTORED', 'warning', {'row': row['id']})
        return True

    # ==================== Main Loop ====================

    async def run_cycle(self, cycle: int, mode: str):
        """One guardian cycle for the given mode (see the module docstring)."""
        if mode == 'enforce':
            await self.invariant_pass(full=(cycle % INVARIANT_SCAN_EVERY_N_CYCLES == 0), dry=False)
            if cycle % EXCHANGE_SCAN_EVERY_N_CYCLES == 0:
                await self.scan_exchange_for_unprotected(rowless_only=True)
        else:
            await self.recover_db_positions()
            if mode == 'shadow' and cycle % INVARIANT_SCAN_EVERY_N_CYCLES == 0:
                await self.invariant_pass(full=True, dry=True)
            if cycle % EXCHANGE_SCAN_EVERY_N_CYCLES == 0:
                await self.scan_exchange_for_unprotected()
        await self._report_expired_tokens()

    async def _report_expired_tokens(self):
        """A6: an in-flight token that aged out while its execute_trade was still running is a critical alarm."""
        for event in follower_guard.drain_expired():
            await self._alarm({'id': event.get('user_id')}, '*', 'ALARM_INFLIGHT_TOKEN_EXPIRED', 'critical',
                              {'token': event.get('token'), 'age_s': event.get('age_s')})

    async def run(self):
        """Main guardian loop."""
        mode = follower_guard.guardian_mode(MODES)
        logger.info("=" * 60)
        logger.info(f"🛡️ POSITION GUARDIAN v2.0 STARTED (GUARDIAN_MODE={mode})")
        logger.info("=" * 60)
        logger.info(f"🔄 Cycle: {GUARDIAN_INTERVAL_SECONDS}s, invariant every {INVARIANT_SCAN_EVERY_N_CYCLES} cycle(s), "
                    f"exchange scan every {EXCHANGE_SCAN_EVERY_N_CYCLES} cycles")
        logger.info("=" * 60)

        if mode == 'enforce':
            try:
                await self.invariant_pass(full=True, dry=False)
                await self.scan_exchange_for_unprotected(rowless_only=True)
            except Exception as e:
                logger.error(f"❌ Enforce startup pass failed: {e}")
        else:
            await self.startup_recovery()

        cycle = 0
        while True:
            try:
                cycle += 1
                t0 = time.monotonic()
                await self.run_cycle(cycle, mode)
                if mode == 'enforce':
                    await asyncio.sleep(max(5.0, self.cadence_s - (time.monotonic() - t0)))
                else:
                    await asyncio.sleep(GUARDIAN_INTERVAL_SECONDS)

            except asyncio.CancelledError:
                logger.info("🛑 Position guardian cancelled")
                break

            except Exception as e:
                logger.error(f"❌ Error in guardian loop: {e}")
                import traceback
                traceback.print_exc()
                await _log_error_to_db(
                    self.db_pool, "system", "GUARDIAN_LOOP_ERROR",
                    str(e), {"cycle": cycle, "traceback": traceback.format_exc()[:500]}
                )
                await asyncio.sleep(10)


async def start_position_guardian(db_pool):
    """Start the position guardian (call from main.py startup)."""
    await asyncio.sleep(45)  # Start after position monitor (40s)

    guardian = PositionGuardian(db_pool)
    await guardian.run()

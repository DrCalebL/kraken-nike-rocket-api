"""
asyncpg-pool stand-in for the Wave 1 follower tests (no database, no asyncpg import).

FakePool records every statement as (method, whitespace-normalised sql, args) and answers
the ones a handler is registered for (`on(needle, fn)`, latest registration wins; the
default answers are execute -> 'OK', fetch -> [], fetchval/fetchrow -> None).
`fail_on(needle, exc)` makes matching statements raise.

FollowerDB registers handlers with the semantics the guardian, the trading loop and
position_monitor rely on: open_positions (incl. status-guarded UPDATEs), follower_users,
signal_deliveries.acknowledged_at, error_logs and trades.
"""
import itertools
from datetime import datetime, timedelta, timezone

ACTIVE = ('open', 'needs_recovery')


def _norm(sql):
    return ' '.join(str(sql).split())


def utcnow_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class _NullTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, pool):
        self.pool = pool

    async def execute(self, sql, *args):
        return await self.pool._dispatch('execute', sql, args)

    async def fetch(self, sql, *args):
        return await self.pool._dispatch('fetch', sql, args)

    async def fetchval(self, sql, *args):
        return await self.pool._dispatch('fetchval', sql, args)

    async def fetchrow(self, sql, *args):
        return await self.pool._dispatch('fetchrow', sql, args)

    def transaction(self):
        return _NullTransaction()


class _Acquire:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        return FakeConn(self.pool)

    async def __aexit__(self, *exc):
        return False


class FakePool:
    DEFAULTS = {'execute': 'OK', 'fetch': [], 'fetchval': None, 'fetchrow': None}

    def __init__(self):
        self.calls = []
        self._handlers = []
        self._fail = []

    def acquire(self):
        return _Acquire(self)

    def on(self, needle, fn):
        self._handlers.insert(0, (_norm(needle), fn))

    def fail_on(self, needle, exc):
        self._fail.append((_norm(needle), exc))

    async def _dispatch(self, method, sql, args):
        flat = _norm(sql)
        self.calls.append((method, flat, args))
        for needle, exc in self._fail:
            if needle in flat:
                raise exc
        for needle, fn in self._handlers:
            if needle in flat:
                return fn(method, flat, args)
        default = self.DEFAULTS[method]
        return list(default) if isinstance(default, list) else default

    def sql(self, needle):
        n = _norm(needle)
        return [c for c in self.calls if n in c[1]]

    def mutations(self):
        """Every INSERT/UPDATE/DELETE except error_logs rows (alarms are not mutations of trading state)."""
        return [c for c in self.calls if c[0] == 'execute' and 'INSERT INTO error_logs' not in c[1]]


class FollowerDB:
    """In-memory follower tables behind a FakePool."""

    # Needles are substrings of the whitespace-normalised statements the code under test issues
    # (position_guardian's SQL_* constants, hosted_trading_loop and position_monitor inline SQL).
    BUSY = "FROM signal_deliveries WHERE user_id = $1 AND acknowledged_at > NOW() - INTERVAL '180 seconds'"
    ROWS_FOR_USER = "status IN ('open', 'needs_recovery') OR opened_at > NOW() - INTERVAL '7 days'"
    USERS_WITH_ACTIVE_ROWS = "ORDER BY MIN(op.opened_at)"
    ACTIVE_USERS = ("FROM follower_users WHERE agent_active = true AND credentials_set = true "
                    "AND kraken_api_key_encrypted IS NOT NULL")
    RECENTLY_CLOSED_USERS = "op.last_fill_at > NOW() - INTERVAL '60 minutes'"
    NEEDS_RECOVERY_ROWS = "WHERE op.status = 'needs_recovery'"
    TRACKED_OPEN_ROWS = "AND tp_order_id IS NOT NULL AND sl_order_id IS NOT NULL"
    OTHER_ACTIVE_ROWS = "AND kraken_symbol = $2 AND status IN ('open', 'needs_recovery') AND id <> $3"
    SET_SL = "SET sl_order_id = $1 WHERE id = $2 AND status IN ('open', 'needs_recovery')"
    SET_TP = "SET tp_order_id = $1 WHERE id = $2 AND status IN ('open', 'needs_recovery')"
    RESTORE_OPEN = "SET status = 'open', quantity = $2"
    MARK_STATUS = "SET status = $1, last_fill_at = NOW() WHERE id = $2 AND status = 'needs_recovery'"

    def __init__(self, pool, users=(), rows=(), now=None):
        self.pool = pool
        self.users = [dict(u) for u in users]
        self.rows = [dict(r) for r in rows]
        self.acks = []                  # (user_id, acknowledged_at naive UTC)
        self.deliveries = {}            # signal_deliveries.id -> user_id (acknowledge_signal)
        self.errors = []                # (error_type, message, context json)
        self.trades = []
        self.now = now or utcnow_naive
        self.signal_db_id = 77
        self._ids = itertools.count(1000)
        pool.on(self.BUSY, self._busy)
        pool.on(self.ROWS_FOR_USER, self._rows_for_user)
        pool.on(self.USERS_WITH_ACTIVE_ROWS, self._users_with_active_rows)
        pool.on(self.ACTIVE_USERS, self._active_users)
        pool.on(self.RECENTLY_CLOSED_USERS, self._recently_closed_users)
        pool.on(self.NEEDS_RECOVERY_ROWS, self._needs_recovery_rows)
        pool.on(self.TRACKED_OPEN_ROWS, self._tracked_open_rows)
        pool.on(self.OTHER_ACTIVE_ROWS, self._other_active_rows)
        pool.on(self.SET_SL, lambda m, s, a: self._guarded(a[1], ACTIVE, sl_order_id=a[0]))
        pool.on(self.SET_TP, lambda m, s, a: self._guarded(a[1], ACTIVE, tp_order_id=a[0]))
        pool.on(self.RESTORE_OPEN, self._restore_open)
        pool.on(self.MARK_STATUS, lambda m, s, a: self._guarded(a[1], ('needs_recovery',), status=a[0],
                                                                  last_fill_at=self.now()))
        pool.on('INSERT INTO error_logs', self._error_log)
        pool.on('INSERT INTO open_positions', self._insert_row)
        pool.on('UPDATE signal_deliveries', self._ack)
        pool.on('SELECT id FROM signals WHERE signal_id', lambda m, s, a: self.signal_db_id)
        pool.on('SELECT COUNT(*) FROM open_positions WHERE user_id = $1 AND signal_id = $2',
                lambda m, s, a: sum(1 for r in self.rows if r['user_id'] == a[0] and r.get('signal_id') == a[1]))
        pool.on('SELECT COUNT(*) FROM trades', lambda m, s, a: 0)
        pool.on('INSERT INTO trades', self._insert_trade)
        pool.on("SET status = 'closed', last_fill_at = NOW() WHERE id = $1",
                lambda m, s, a: self._guarded(a[0], None, status='closed', last_fill_at=self.now()))

    # ---------- helpers for tests ----------
    def add_user(self, user_id, **fields):
        u = {'id': user_id, 'api_key': 'nk_user_%d_abcdefghijkl' % user_id, 'email': 'u%d@example.com' % user_id,
             'kraken_api_key_encrypted': 'enc-k', 'kraken_api_secret_encrypted': 'enc-s',
             'agent_active': True, 'credentials_set': True}
        u.update(fields)
        self.users.append(u)
        return u

    def add_row(self, **fields):
        r = {'id': next(self._ids), 'signal_id': self.signal_db_id, 'entry_order_id': 'ord-entry',
             'tp_order_id': None, 'sl_order_id': None, 'symbol': 'ADA/USDT', 'kraken_symbol': 'PF_ADAUSD',
             'side': 'BUY', 'quantity': 100.0, 'leverage': 5.0, 'entry_fill_price': 0.5,
             'target_tp': 0.52, 'target_sl': 0.49, 'opened_at': self.now() - timedelta(minutes=10),
             'status': 'open', 'last_fill_at': None}
        r.update(fields)
        self.rows.append(r)
        return r

    def row(self, row_id):
        return next(r for r in self.rows if r['id'] == row_id)

    def ack(self, user_id, when=None):
        self.acks.append((user_id, when or self.now()))

    def error_types(self):
        return [e[0] for e in self.errors]

    # ---------- handlers ----------
    def _busy(self, method, sql, args):
        user_id, since = args
        floor = self.now() - timedelta(seconds=180)
        return any(uid == user_id and at > floor and (since is None or at > since) for uid, at in self.acks)

    def _rows_for_user(self, method, sql, args):
        floor = self.now() - timedelta(days=7)
        rows = [dict(r) for r in self.rows if r['user_id'] == args[0]
                and (r['status'] in ACTIVE or (r['opened_at'] is not None and r['opened_at'] > floor))]
        return sorted(rows, key=lambda r: r['opened_at'], reverse=True)

    def _user_view(self, u):
        return {k: u[k] for k in ('id', 'api_key', 'email', 'kraken_api_key_encrypted', 'kraken_api_secret_encrypted')}

    def _users_with_active_rows(self, method, sql, args):
        first = {}
        for r in self.rows:
            if r['status'] in ACTIVE:
                first[r['user_id']] = min(first.get(r['user_id'], r['opened_at']), r['opened_at'])
        users = [u for u in self.users if u['id'] in first and u.get('kraken_api_key_encrypted')]
        return [self._user_view(u) for u in sorted(users, key=lambda u: first[u['id']])]

    def _active_users(self, method, sql, args):
        return [self._user_view(u) for u in self.users
                if u.get('agent_active') and u.get('credentials_set') and u.get('kraken_api_key_encrypted')]

    def _recently_closed_users(self, method, sql, args):
        floor = self.now() - timedelta(minutes=60)
        ids = {r['user_id'] for r in self.rows
               if r['status'] not in ACTIVE and r.get('last_fill_at') and r['last_fill_at'] > floor}
        return [self._user_view(u) for u in self.users if u['id'] in ids and u.get('kraken_api_key_encrypted')]

    def _needs_recovery_rows(self, method, sql, args):
        out = []
        for r in self.rows:
            if r['status'] != 'needs_recovery':
                continue
            u = next(u for u in self.users if u['id'] == r['user_id'])
            out.append(dict(r, api_key=u['api_key'], email=u['email'],
                            kraken_api_key_encrypted=u['kraken_api_key_encrypted'],
                            kraken_api_secret_encrypted=u['kraken_api_secret_encrypted']))
        return out

    def _tracked_open_rows(self, method, sql, args):
        return sum(1 for r in self.rows if r['user_id'] == args[0] and r['status'] == 'open'
                   and r.get('tp_order_id') and r.get('sl_order_id'))

    def _other_active_rows(self, method, sql, args):
        user_id, kraken_symbol, row_id = args
        return sum(1 for r in self.rows if r['user_id'] == user_id and r['kraken_symbol'] == kraken_symbol
                   and r['status'] in ACTIVE and r['id'] != row_id)

    def _guarded(self, row_id, statuses, **fields):
        for r in self.rows:
            if r['id'] == row_id and (statuses is None or r['status'] in statuses):
                r.update(fields)
                return 'UPDATE 1'
        return 'UPDATE 0'

    def _restore_open(self, method, sql, args):
        row_id, qty, entry = args
        r = next((r for r in self.rows if r['id'] == row_id), None)
        if r is None or r['status'] != 'needs_recovery':
            return 'UPDATE 0'
        r.update(status='open', quantity=qty, entry_fill_price=entry if entry is not None else r['entry_fill_price'])
        return 'UPDATE 1'

    def _error_log(self, method, sql, args):
        self.errors.append((args[1], args[2], args[3]))
        return 'INSERT 0 1'

    def _insert_row(self, method, sql, args):
        cols = ('user_id', 'signal_id', 'entry_order_id', 'tp_order_id', 'sl_order_id', 'symbol',
                'kraken_symbol', 'side', 'quantity', 'leverage', 'entry_fill_price', 'target_tp', 'target_sl')
        row = dict(zip(cols, args[:13]))
        if len(args) == 15:
            row.update(opened_at=args[13], status=args[14])
        else:
            row.update(opened_at=self.now(), status='needs_recovery')
        row.update(id=next(self._ids), last_fill_at=None)
        self.rows.append(row)
        return 'INSERT 0 1'

    def _ack(self, method, sql, args):
        self.acks.append((self.deliveries.get(args[0]), self.now()))
        return 'UPDATE 1'

    def _insert_trade(self, method, sql, args):
        self.trades.append(args)
        return 'INSERT 0 1'

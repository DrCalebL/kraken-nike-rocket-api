"""
Offline close-job tables for the T08 master CLOSE tests (no database): master_closes + close_jobs behind the
FakePool/FollowerDB of tests/fakes/fake_pool.py.

CloseDB registers handlers for the exact SQL constants hosted_trading_loop and position_guardian issue on the close
path (the needles are those constants, whitespace-normalised by FakePool), with the semantics the code relies on:
status-guarded job updates, the status-guarded exit_hint write, the cumulative close_fill_qty write and the
guardian's close-job busy check.
"""
from datetime import timedelta

import close_guard as cg
import hosted_trading_loop as htl
import position_guardian as pg

ACTIVE = ('open', 'needs_recovery')


class CloseDB:
    def __init__(self, db):
        self.db = db
        self.pool = db.pool
        self.closes = {}                # master_trade_id -> {'symbol', 'side', 'reason', 'master_closed_ms', ...}
        self.jobs = {}                  # position_id -> job dict
        p = self.pool
        p.on(htl.SQL_CLOSE_JOBS_DUE, self._jobs_due)
        p.on(htl.SQL_CLOSE_ROW, self._row)
        p.on(htl.SQL_CLOSE_JOB_SET, self._job_set)
        p.on(htl.SQL_SET_EXIT_HINT, self._exit_hint)
        p.on(htl.SQL_ADD_CLOSE_FILL, self._add_fill)
        p.on(pg.SQL_CLOSE_BUSY, self._busy)

    # ---------- helpers for tests ----------
    def add_close(self, master_trade_id, side='BUY', symbol='ADA/USDT', matched=1):
        self.closes[master_trade_id] = {'master_trade_id': master_trade_id, 'symbol': symbol, 'side': side,
                                        'reason': 'time_exit', 'master_closed_ms': 1_790_000_000_000,
                                        'received_at': self.db.now(), 'matched': matched}

    def add_job(self, row, master_trade_id='0123456789abcdef0123456789abcdef', side=None, status='pending',
                age_s=5, attempts=0):
        if master_trade_id not in self.closes:
            self.add_close(master_trade_id, side=side or row['side'])
        created = self.db.now() - timedelta(seconds=age_s)
        job = {'position_id': row['id'], 'user_id': row['user_id'], 'master_trade_id': master_trade_id,
               'status': status, 'attempts': attempts, 'last_error': None, 'created_at': created,
               'updated_at': created}
        self.jobs[row['id']] = job
        return job

    def job(self, position_id):
        return self.jobs[position_id]

    # ---------- handlers ----------
    def _jobs_due(self, method, sql, args):
        out = []
        for j in sorted(self.jobs.values(), key=lambda j: j['created_at']):
            if j['status'] not in cg.JOB_OPEN_STATUSES:
                continue
            mc = self.closes[j['master_trade_id']]
            u = next(u for u in self.db.users if u['id'] == j['user_id'])
            out.append(dict(j, payload_side=mc['side'], payload_symbol=mc['symbol'], api_key=u['api_key'],
                            email=u['email'], kraken_api_key_encrypted=u.get('kraken_api_key_encrypted'),
                            kraken_api_secret_encrypted=u.get('kraken_api_secret_encrypted'),
                            agent_active=u.get('agent_active'), credentials_set=u.get('credentials_set'),
                            access_granted=u.get('access_granted', True),
                            pending_invoice_id=u.get('pending_invoice_id'),
                            invoice_due_date=u.get('invoice_due_date')))
        return out

    def _row(self, method, sql, args):
        r = next((r for r in self.db.rows if r['id'] == args[0]), None)
        if r is None:
            return None
        out = dict(r)
        out.setdefault('exit_hint', None)
        out.setdefault('close_fill_qty', None)
        return out

    def _job_set(self, method, sql, args):
        position_id, status, attempts, last_error = args
        j = self.jobs.get(position_id)
        if j is None or j['status'] not in cg.JOB_OPEN_STATUSES:
            return 'UPDATE 0'
        j.update(status=status, attempts=attempts, last_error=last_error, updated_at=self.db.now())
        return 'UPDATE 1'

    def _exit_hint(self, method, sql, args):
        r = next((r for r in self.db.rows if r['id'] == args[0]), None)
        if r is None or r['status'] not in ACTIVE:
            return 'UPDATE 0'
        r['exit_hint'] = 'TIME'
        return 'UPDATE 1'

    def _add_fill(self, method, sql, args):
        r = next((r for r in self.db.rows if r['id'] == args[0]), None)
        if r is None:
            return 'UPDATE 0'
        r['close_fill_qty'] = float(r.get('close_fill_qty') or 0) + float(args[1])
        return 'UPDATE 1'

    def _busy(self, method, sql, args):
        user_id = args[0]
        floor = self.db.now() - timedelta(seconds=cg.CLOSE_BUSY_WINDOW_S)
        return any(j['user_id'] == user_id and (j['status'] == 'pending'
                                                or (j['status'] == 'done' and j['updated_at'] > floor))
                   for j in self.jobs.values())

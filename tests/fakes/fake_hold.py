"""
Offline hold tables for the T1LIVE-7 tests: the statements hosted_trading_loop / position_monitor issue for the BNB
hold, behind the FakePool/FollowerDB of tests/fakes/fake_pool.py (needles are the module constants).
"""
import hosted_trading_loop as htl
import position_monitor as pm

OTHER = None


class HoldDB:
    def __init__(self, db):
        self.db = db
        self.pool = db.pool
        self.closed = set()             # master_trade_ids the master closed
        self.sitouts = set()            # (user_id, master_trade_id)
        self.pending = []               # SQL_PENDING_HOLDS rows
        self.mtid_of_signal = {}        # signals.id -> master_trade_id
        p = self.pool
        p.on(htl.SQL_OTHER_KIND_ROWS, self._other_kind_rows)
        p.on(htl.SQL_INSERT_HOLD_ROW, self._insert_hold)
        p.on(htl.SQL_INSERT_HOLD_RECOVERY, self._insert_hold_recovery)
        p.on(htl.SQL_HOLD_CLOSED, lambda m, s, a: 1 if a[0] in self.closed else None)
        p.on(htl.SQL_HOLD_SITOUT, lambda m, s, a: 1 if (a[0], a[1]) in self.sitouts else None)
        p.on(htl.SQL_PENDING_HOLDS, lambda m, s, a: [dict(r) for r in self.pending])
        p.on(pm.SQL_HOLD_SITOUT_INSERT, self._sitout_insert)
        base = db.signal_db_id
        p.on('SELECT id FROM signals WHERE signal_id', lambda m, s, a: 88 if str(a[0]).startswith('sig-hold') else base)

    def _other_kind_rows(self, method, sql, args):
        user_id, strategy = args
        return [dict(r, kind=r.get('kind') or 't1') for r in self.db.rows
                if r['user_id'] == user_id and r['status'] == 'open' and (r.get('kind') or 't1') != strategy]

    def _insert_hold(self, method, sql, args):
        (user_id, signal_id, entry_order_id, sl_order_id, symbol, kraken_symbol, qty, lev, fill, sl, opened) = args
        self.db.rows.append({
            'id': next(self.db._ids), 'user_id': user_id, 'signal_id': signal_id, 'entry_order_id': entry_order_id,
            'tp_order_id': None, 'sl_order_id': sl_order_id, 'symbol': symbol, 'kraken_symbol': kraken_symbol,
            'side': 'BUY', 'quantity': qty, 'leverage': lev, 'entry_fill_price': fill, 'target_tp': None,
            'target_sl': sl, 'opened_at': opened, 'status': 'open', 'kind': 'hold', 'last_fill_at': None})
        return 'INSERT 0 1'

    def _insert_hold_recovery(self, method, sql, args):
        (user_id, signal_id, entry_order_id, sl_order_id, symbol, kraken_symbol, side, qty, lev, fill, sl) = args
        self.db.rows.append({
            'id': next(self.db._ids), 'user_id': user_id, 'signal_id': signal_id, 'entry_order_id': entry_order_id,
            'tp_order_id': None, 'sl_order_id': sl_order_id, 'symbol': symbol, 'kraken_symbol': kraken_symbol,
            'side': side, 'quantity': qty, 'leverage': lev, 'entry_fill_price': fill, 'target_tp': None,
            'target_sl': sl, 'opened_at': self.db.now(), 'status': 'needs_recovery', 'kind': 'hold',
            'last_fill_at': None})
        return 'INSERT 0 1'

    def _sitout_insert(self, method, sql, args):
        user_id, signal_id, reason = args
        self.sitouts.add((user_id, self.mtid_of_signal.get(signal_id, 'mtid-of-%s' % signal_id)))
        return 'INSERT 0 1'

    def add_delivery(self, user, mtid='0123456789abcdef0123456789abcdef', offer_ms=None, delivery_id=None,
                     ref=600.0, stop=570.0, r_b=0.02, n2=0.5, signal_db_id=88):
        import time
        delivery_id = delivery_id or 7000 + user['id']
        self.db.deliveries[delivery_id] = user['id']
        self.mtid_of_signal[signal_db_id] = mtid
        row = {'user_id': user['id'], 'api_key': user['api_key'], 'email': user['email'],
               'kraken_api_key_encrypted': 'enc-k', 'kraken_api_secret_encrypted': 'enc-s',
               'delivery_id': delivery_id, 'offer_ms': offer_ms if offer_ms is not None else int(time.time() * 1000),
               'signal_db_id': signal_db_id, 'signal_id': 'sig-hold-%d' % user['id'], 'master_trade_id': mtid,
               'symbol': 'BNB/USDT', 'entry_price': ref, 'stop_loss': stop, 'hold_r_b': r_b, 'hold_n2': n2,
               'hold_leader_trade_id': 'L1'}
        self.pending.append(row)
        return row

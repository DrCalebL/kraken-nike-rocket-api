"""
Offline, stateful fake of ccxt.krakenfutures for the Wave 1 follower tests.

It encodes the verified krakenfutures gotchas (CLAUDE.md):
- fetch_positions() no-arg returns unified symbols ('ADA/USD:USD'); a symbols list only
  matches unified symbols, so fetch_positions(['PF_ADAUSD']) returns [] (gotcha 2);
- there is no fetch_order (ccxt.NotSupported, gotcha 3);
- a reduce-only order with no position to reduce is rejected (gotcha 4);
- open stop orders carry their level ONLY in info.stopPrice (unified triggerPrice /
  stopPrice are None, the ccxt 4.4.97 openorders shape) and info.symbol is lower-case;
- positions carry the ccxt 'timestamp' (Kraken fillTime, ms); contractSize is the
  market multiplier (1.0), never the position size.

One FakeKrakenFutures per follower account; a FakeVenue shares the call log, the per-call
latency and the concurrency counter across accounts (load tests).
"""
import contextlib
import itertools
import math
import threading
import time

import ccxt

MARKETS = {
    'ADA/USD:USD': {'id': 'PF_ADAUSD', 'symbol': 'ADA/USD:USD', 'precision': {'price': 1e-05, 'amount': 1.0}},
    'BTC/USD:USD': {'id': 'PF_XBTUSD', 'symbol': 'BTC/USD:USD', 'precision': {'price': 0.5, 'amount': 0.0001}},
}


class FakeVenue:
    def __init__(self, latency_s=0.0):
        self.latency_s = latency_s
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.calls = []                  # (account, method, info dict, thread name)
        self._ids = itertools.count(1)
        self.accounts = {}

    def account(self, name, **kwargs):
        if name not in self.accounts:
            self.accounts[name] = FakeKrakenFutures(venue=self, name=name, **kwargs)
        return self.accounts[name]

    def next_id(self, prefix):
        with self.lock:
            return '%s-%d' % (prefix, next(self._ids))


class FakeKrakenFutures:
    def __init__(self, venue=None, name='acct', prices=None, equity=10000.0, now_ms=None):
        self.venue = venue or FakeVenue()
        self.name = name
        self.markets = dict(MARKETS)
        self.prices = dict(prices or {'ADA/USD:USD': 0.5, 'BTC/USD:USD': 60000.0})
        self.equity = equity
        self.positions = {}              # unified -> {'side', 'contracts', 'entryPrice', 'timestamp'}
        self.orders = {}                 # oid -> ccxt-shaped open order
        self.trades = []                 # ccxt-shaped fills
        self.faults = []
        self.empty_position_reads = 0    # the next N fetch_positions() calls return [] (false-empty reads)
        self.fill_ratio = 1.0            # market entries fill this share of the requested amount
        self.raise_after_entry_fill = 0  # the next N market entries fill, then raise (timeout after fill)
        self.ignore_reduce_only_market = False   # accept reduce-only market closes without filling
        self.leverage = None
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))

    # ---------- test controls ----------
    def fault(self, method, exc, times=1, when=None):
        """The next `times` calls of `method` for which when(info) is true raise `exc`."""
        self.faults.append({'method': method, 'exc': exc, 'left': times, 'when': when or (lambda info: True)})

    def calls(self, method=None):
        return [(m, info) for (acct, m, info, _t) in self.venue.calls
                if acct == self.name and (method is None or m == method)]

    def threads(self):
        return [t for (acct, _m, _i, t) in self.venue.calls if acct == self.name]

    def open_position(self, symbol, side, contracts, entry, ts_ms=None):
        sym = self.market(symbol)['symbol']
        self.positions[sym] = {'side': side, 'contracts': float(contracts), 'entryPrice': float(entry),
                               'timestamp': ts_ms if ts_ms is not None else self.now_ms()}

    def add_order(self, symbol, kind, side, amount, level, reduce_only=True, oid=None, limit=None):
        """kind: 'stop' (stop-market; stop-limit when limit is given), 'lmt' or 'take_profit'."""
        sym = self.market(symbol)['symbol']
        oid = oid or self.venue.next_id('ord')
        stop_px, limit_px = (None, float(level)) if kind == 'lmt' else (float(level), limit)
        self.orders[oid] = self._order_row(oid, sym, kind, side, float(amount), stop_px, limit_px, reduce_only)
        return oid

    def external_cancel(self, oid):
        self.orders.pop(oid, None)

    def set_price(self, symbol, px):
        """Move the last price; resting stops/limits that it crosses fill (no OCO: the other leg stays)."""
        sym = self.market(symbol)['symbol']
        self.prices[sym] = px
        for oid, o in list(self.orders.items()):
            if o['symbol'] != sym or oid not in self.orders:
                continue
            info = o['info']
            if info['orderType'] == 'stop':
                level = info['stopPrice']
                hit = px <= level if o['side'] == 'sell' else px >= level
            elif info['orderType'] == 'take_profit':
                level = info['stopPrice']
                hit = px >= level if o['side'] == 'sell' else px <= level
            else:
                level = info['limitPrice']
                hit = px >= level if o['side'] == 'sell' else px <= level
            if not hit:
                continue
            del self.orders[oid]
            pos = self.positions.get(sym)
            if pos is None and o['reduceOnly']:
                continue
            amount = min(o['remaining'], pos['contracts']) if pos is not None else o['remaining']
            self._fill(sym, o['side'], amount, level, oid, o['reduceOnly'])

    # ---------- ccxt surface ----------
    @contextlib.contextmanager
    def _call(self, method, **info):
        v = self.venue
        with v.lock:
            v.calls.append((self.name, method, info, threading.current_thread().name))
            v.active += 1
            v.peak = max(v.peak, v.active)
        try:
            if v.latency_s:
                time.sleep(v.latency_s)
            for f in self.faults:
                if f['method'] == method and f['left'] > 0 and f['when'](info):
                    f['left'] -= 1
                    raise f['exc']
            yield
        finally:
            with v.lock:
                v.active -= 1

    def market(self, symbol):
        if symbol in self.markets:
            return self.markets[symbol]
        for m in self.markets.values():
            if m['id'].upper() == str(symbol).upper():
                return m
        raise ccxt.BadSymbol('krakenfutures does not have market symbol %s' % symbol)

    def load_markets(self, reload=False, params=None):
        with self._call('load_markets'):
            return self.markets

    def amount_to_precision(self, symbol, amount):
        step = self.market(symbol)['precision']['amount']
        return repr(round(math.floor(float(amount) / step + 1e-9) * step, 10))

    def price_to_precision(self, symbol, price):
        tick = self.market(symbol)['precision']['price']
        return repr(round(round(float(price) / tick) * tick, 10))

    def fetch_balance(self, params=None):
        with self._call('fetch_balance'):
            return {'USD': {'total': self.equity}, 'info': {}}

    def set_leverage(self, leverage, symbol=None, params=None):
        with self._call('set_leverage', leverage=leverage, symbol=symbol):
            self.leverage = leverage

    def fetch_positions(self, symbols=None, params=None):
        with self._call('fetch_positions', symbols=symbols):
            if self.empty_position_reads > 0:
                self.empty_position_reads -= 1
                return []
            rows = [self._position_row(sym) for sym in self.positions]
            if symbols:
                rows = [p for p in rows if p['symbol'] in symbols]      # gotcha 2: PF_ ids never match
            return rows

    def fetch_open_orders(self, symbol=None, since=None, limit=None, params=None):
        with self._call('fetch_open_orders', symbol=symbol):
            rows = [dict(o, info=dict(o['info'])) for o in self.orders.values()]
            if symbol is not None:
                want = self.market(symbol)['symbol']
                rows = [o for o in rows if o['symbol'] == want]
            return rows

    def fetch_ticker(self, symbol, params=None):
        with self._call('fetch_ticker', symbol=symbol):
            sym = self.market(symbol)['symbol']
            return {'symbol': sym, 'last': self.prices[sym]}

    def fetch_my_trades(self, symbol=None, since=None, limit=None, params=None):
        with self._call('fetch_my_trades', symbol=symbol, since=since):
            rows = [dict(t) for t in self.trades]
            if symbol is not None:
                want = self.market(symbol)['symbol']
                rows = [t for t in rows if t['symbol'] == want]
            if since is not None:
                rows = [t for t in rows if t['timestamp'] >= since]
            return rows[-limit:] if limit else rows

    def fetch_order(self, id, symbol=None, params=None):
        raise ccxt.NotSupported('krakenfutures fetchOrder() is not supported yet')    # gotcha 3

    def create_order(self, symbol, type, side, amount, price=None, params=None):
        params = dict(params or {})
        with self._call('create_order', symbol=symbol, type=type, side=side, amount=amount, price=price, params=params):
            return self._create(symbol, type, side, float(amount), price, params)

    def create_market_order(self, symbol, side, amount, price=None, params=None):
        params = dict(params or {})
        with self._call('create_market_order', symbol=symbol, type='market', side=side, amount=amount, params=params):
            return self._create(symbol, 'market', side, float(amount), None, params)

    def create_limit_order(self, symbol, side, amount, price, params=None):
        params = dict(params or {})
        with self._call('create_limit_order', symbol=symbol, type='limit', side=side, amount=amount, price=price,
                        params=params):
            return self._create(symbol, 'limit', side, float(amount), price, params)

    def cancel_order(self, id, symbol=None, params=None):
        with self._call('cancel_order', id=id, symbol=symbol):
            if id not in self.orders:
                raise ccxt.OrderNotFound('krakenfutures: cancelOrder failed due to notFound')
            self.orders.pop(id)
            return {'id': id, 'status': 'canceled', 'info': {}}

    # ---------- internals ----------
    def _create(self, symbol, type, side, amount, price, params):
        sym = self.market(symbol)['symbol']
        reduce_only = bool(params.get('reduceOnly'))
        trigger = params.get('triggerPrice', params.get('stopPrice'))
        pos = self.positions.get(sym)
        same_side = pos is not None and pos['side'] == ('long' if side == 'buy' else 'short')
        if reduce_only and (pos is None or same_side):
            raise ccxt.ExchangeError('krakenfutures: createOrder failed due to wouldNotReducePosition')
        oid = self.venue.next_id('ord')
        if type == 'market' and trigger is None:
            if reduce_only and self.ignore_reduce_only_market:
                return {'id': oid, 'symbol': sym, 'type': 'market', 'side': side, 'amount': amount,
                        'status': 'closed', 'info': {'order_id': oid}}
            filled = amount
            if not reduce_only and self.fill_ratio < 1.0:
                step = self.market(sym)['precision']['amount']
                filled = math.floor(amount * self.fill_ratio / step + 1e-9) * step
            self._fill(sym, side, filled, self.prices[sym], oid, reduce_only)
            if not reduce_only and self.raise_after_entry_fill > 0:
                self.raise_after_entry_fill -= 1
                raise ccxt.RequestTimeout('krakenfutures: request timed out (the order filled)')
            return {'id': oid, 'symbol': sym, 'type': 'market', 'side': side, 'amount': amount,
                    'status': 'closed', 'info': {'order_id': oid}}
        if trigger is not None:
            kind, stop_px, limit_px = 'stop', float(trigger), (float(price) if price is not None else None)
        elif type == 'limit':
            kind, stop_px, limit_px = 'lmt', None, float(price)
        else:
            raise ccxt.InvalidOrder('krakenfutures: createOrder failed due to invalidOrderType')
        self.orders[oid] = self._order_row(oid, sym, kind, side, amount, stop_px, limit_px, reduce_only)
        return {'id': oid, 'symbol': sym, 'type': 'stop' if kind == 'stop' else 'limit', 'side': side,
                'amount': amount, 'status': 'open', 'info': {'order_id': oid, 'status': 'placed'}}

    def _order_row(self, oid, sym, kind, side, amount, stop_px, limit_px, reduce_only):
        m = self.market(sym)
        return {
            'id': oid, 'symbol': sym, 'type': 'limit' if kind == 'lmt' else kind, 'side': side,
            'amount': amount, 'remaining': amount, 'filled': 0.0, 'price': None,
            'triggerPrice': None, 'stopPrice': None, 'reduceOnly': reduce_only, 'status': 'open',
            'info': {'order_id': oid, 'symbol': m['id'].lower(), 'side': side, 'orderType': kind,
                     'stopPrice': stop_px, 'limitPrice': limit_px, 'unfilledSize': amount, 'filledSize': 0,
                     'reduceOnly': reduce_only, 'triggerSignal': 'last_price'},
        }

    def _position_row(self, sym):
        p = self.positions[sym]
        m = self.market(sym)
        return {'symbol': sym, 'side': p['side'], 'contracts': p['contracts'], 'contractSize': 1.0,
                'entryPrice': p['entryPrice'], 'timestamp': p['timestamp'],
                'info': {'symbol': m['id'].lower(), 'side': p['side'], 'size': str(p['contracts']),
                         'price': str(p['entryPrice'])}}

    def _fill(self, sym, side, amount, px, oid, reduce_only):
        if amount <= 0:
            return
        want = 'long' if side == 'buy' else 'short'
        pos = self.positions.get(sym)
        ts = self.now_ms()
        if pos is None:
            self.positions[sym] = {'side': want, 'contracts': amount, 'entryPrice': px, 'timestamp': ts}
        elif pos['side'] == want:
            total = pos['contracts'] + amount
            pos['entryPrice'] = (pos['entryPrice'] * pos['contracts'] + px * amount) / total
            pos['contracts'] = total
        else:
            pos['contracts'] = round(pos['contracts'] - min(amount, pos['contracts']), 10)
            if pos['contracts'] <= 0:
                del self.positions[sym]
        self.trades.append({'id': self.venue.next_id('fill'), 'order': oid, 'timestamp': ts, 'symbol': sym,
                            'side': side, 'price': px, 'amount': amount, 'info': {}})

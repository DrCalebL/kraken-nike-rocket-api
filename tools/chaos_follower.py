#!/usr/bin/env python
"""
Owner-run chaos test of the Kraken follower guardian on the Kraken Futures DEMO venue
(spec C39 / T30 / I14). Builders never run it against a venue; only the owner does.

  ROCKET_TESTNET=1 python tools/chaos_follower.py --cases a,b,c,d,e --runs 20 --i-accept-demo-orders
  python tools/chaos_follower.py --fake --cases a,b,c,d,e --runs 2      # offline self-test, no network

Interlocks (checked before any network call; any failure exits 2):
- --i-accept-demo-orders and ROCKET_TESTNET=1 (not needed with --fake);
- FOLLOWER_API_URL, MASTER_API_KEY and DATABASE_URL are unset (no production API or DB);
- keys come only from CHAOS_KRAKEN_DEMO_KEY / CHAOS_KRAKEN_DEMO_SECRET and are never printed;
- ccxt.krakenfutures runs in sandbox mode and every DNS lookup outside DEMO_HOSTS is refused;
- GUARDIAN_MODE is forced to 'enforce' in-process; the store is an in-memory FakePool/FollowerDB
  (one synthetic user and row per run); notify_admin is patched to print;
- CHAOS_MAX_NOTIONAL (USD, default 50) caps each position; the account must be flat with no open
  orders before every run; a finally/SIGINT cleanup cancels the run's orders and closes the position.

Cases (one consistent row, SL 3% and TP 3% from the entry):
  a  TP cancelled externally                 -> TP restored
  b  SL cancelled externally                 -> SL restored
  c  both cancelled                          -> SL restored (first), then TP
  d  row SL moved beyond the market + SL cancelled -> position closed
  e  needs_recovery row, live position, no legs    -> SL + TP placed, row restored to 'open'
PASS per case: every run restored (d: closed) within --max-wait (330 s), SL-absent <= 330 s and
zero closes of a position that had both legs live. Outputs chaos_out/<UTC>/runs.csv, summary.json,
shapes.json.
"""
import argparse
import asyncio
import csv
import json
import os
import random
import socket
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DEMO_HOSTS = ('demo-futures.kraken.com',)
DEFAULT_SYMBOL = 'ADA/USD:USD'
SL_FRACTION = 0.03
TP_FRACTION = 0.03
POLL_S = 10.0


def refusals(args, env):
    out = []
    for name in ('FOLLOWER_API_URL', 'MASTER_API_KEY', 'DATABASE_URL'):
        if env.get(name):
            out.append('%s is set - unset it: chaos must never reach production' % name)
    if args.fake:
        return out
    if not args.i_accept_demo_orders:
        out.append('missing --i-accept-demo-orders')
    if env.get('ROCKET_TESTNET') != '1':
        out.append('ROCKET_TESTNET=1 is required')
    if not env.get('CHAOS_KRAKEN_DEMO_KEY') or not env.get('CHAOS_KRAKEN_DEMO_SECRET'):
        out.append('CHAOS_KRAKEN_DEMO_KEY / CHAOS_KRAKEN_DEMO_SECRET are required')
    return out


def install_dns_allowlist(hosts):
    real = socket.getaddrinfo

    def guarded(host, *args, **kwargs):
        if host not in hosts:
            raise OSError('chaos_follower: DNS lookup of %r refused (allowlist %s)' % (host, ', '.join(hosts)))
        return real(host, *args, **kwargs)

    socket.getaddrinfo = guarded


class Clock:
    """Real time, or simulated time for --fake (sleep advances the clock instantly)."""

    def __init__(self, fake):
        self.fake = fake
        self.t = 1_790_000_000.0

    def time(self):
        return self.t if self.fake else time.time()

    def utcnow(self):
        return datetime.fromtimestamp(self.time(), timezone.utc).replace(tzinfo=None)

    async def sleep(self, seconds, result=None):
        if self.fake:
            self.t += seconds
            await asyncio.sleep(0)
        else:
            await asyncio.sleep(seconds)
        return result


def make_exchange(args, clock):
    if args.fake:
        from tests.fakes.fake_krakenfutures import FakeKrakenFutures
        return FakeKrakenFutures(name='chaos', now_ms=lambda: int(clock.time() * 1000))
    import ccxt
    install_dns_allowlist(DEMO_HOSTS)
    ex = ccxt.krakenfutures({'apiKey': os.environ['CHAOS_KRAKEN_DEMO_KEY'],
                             'secret': os.environ['CHAOS_KRAKEN_DEMO_SECRET'], 'enableRateLimit': True})
    ex.set_sandbox_mode(True)
    ex.load_markets()
    return ex


def book(ex, symbol):
    """(position or None, [ccxt open orders on symbol]) from no-arg reads."""
    pos = next((p for p in ex.fetch_positions() if p.get('symbol') == symbol and abs(float(p.get('contracts') or 0)) > 0),
               None)
    orders = [o for o in ex.fetch_open_orders() if o.get('symbol') == symbol]
    return pos, orders


def classify(ex, symbol, row, market):
    import bracket_guard as bg
    pos, orders = book(ex, symbol)
    tick = float(market['precision']['price']) * bg.TICK_TOL
    legs = [bg.leg_from_kraken(o) for o in orders if bg.kraken_order_matches(o, market['symbol'], market['id'])]
    sl_ok = any(l.kind == bg.STOP_MARKET and l.trigger is not None and abs(l.trigger - row['target_sl']) <= tick
                for l in legs)
    tp_ok = any(l.kind in bg.TP_KINDS and l.level is not None and abs(l.level - row['target_tp']) <= tick
                for l in legs)
    return pos, sl_ok, tp_ok


async def one_run(case, run, args, ex, clock, shapes):
    import position_guardian as pg
    from tests.fakes.fake_pool import FakePool, FollowerDB
    symbol = args.symbol
    market = ex.market(symbol)
    pos, orders = book(ex, symbol)
    if pos is not None or ex.fetch_open_orders():
        raise SystemExit(3)
    last = float(ex.fetch_ticker(symbol)['last'])
    qty = float(ex.amount_to_precision(symbol, float(os.getenv('CHAOS_MAX_NOTIONAL', '50')) / last))
    ex.create_order(symbol, 'market', 'buy', qty)
    for _ in range(8):
        pos, _ = book(ex, symbol)
        if pos is not None:
            break
        await clock.sleep(1)
    if pos is None:
        raise RuntimeError('entry not confirmed')
    lot = float(market['precision']['amount'])
    if not shapes:
        shapes['openorders_before_bracket'] = [o.get('info') for o in ex.fetch_open_orders()]
        shapes['position'] = pos.get('info')
        shapes['position_timestamp_before'] = pos.get('timestamp')
        if qty >= 2 * lot:                                  # does Kraken fillTime move on a partial reduction?
            ex.create_order(symbol, 'market', 'sell', lot, None, {'reduceOnly': True})
            pos, _ = book(ex, symbol)
            shapes['position_timestamp_after_partial_reduce'] = pos.get('timestamp') if pos else None
            qty = abs(float(pos['contracts'])) if pos else qty
    entry = float(pos.get('entryPrice') or last)
    sl = float(ex.price_to_precision(symbol, entry * (1 - SL_FRACTION)))
    tp = float(ex.price_to_precision(symbol, entry * (1 + TP_FRACTION)))
    sl_oid = str(ex.create_order(symbol, 'stop', 'sell', qty, None, {'triggerPrice': sl, 'reduceOnly': True})['id'])
    tp_oid = str(ex.create_order(symbol, 'limit', 'sell', qty, tp, {'reduceOnly': True})['id'])
    if 'openorders' not in shapes:
        shapes['openorders'] = [o.get('info') for o in ex.fetch_open_orders()]
    pool = FakePool()
    db = FollowerDB(pool, now=clock.utcnow)
    user = db.add_user(990000 + run)
    row = db.add_row(user_id=user['id'], symbol='ADA/USDT', kraken_symbol=market['id'], side='BUY', quantity=qty,
                     entry_fill_price=entry, target_sl=sl, target_tp=tp, sl_order_id=sl_oid, tp_order_id=tp_oid,
                     opened_at=clock.utcnow() - timedelta(seconds=30))
    guardian = pg.PositionGuardian(pool, clock=clock.time, sleep=clock.sleep)
    guardian.active_exchanges[user['api_key']] = ex
    if case in ('a', 'c', 'e'):
        ex.cancel_order(tp_oid, symbol)
    if case in ('b', 'c', 'd', 'e'):
        ex.cancel_order(sl_oid, symbol)
    if case == 'd':
        row['target_sl'] = float(ex.price_to_precision(symbol, last * 1.01))      # beyond: the market is below
    if case == 'e':
        row.update(status='needs_recovery', sl_order_id=None, tp_order_id=None)
    t0 = clock.time()
    next_pass = t0 + random.Random(run).uniform(0, args.guardian_period)   # the real cadence: a random phase
    sl_absent_since = None if case in ('a',) else t0
    sl_absent_max = 0.0
    restored_s, closed, false_close = None, False, False
    try:
        while clock.time() - t0 <= args.max_wait:
            if clock.time() >= next_pass:
                await guardian.invariant_pass(full=False, dry=False)
                next_pass = clock.time() + args.guardian_period
            live, sl_ok, tp_ok = classify(ex, symbol, db.row(row['id']), market)
            now = clock.time()
            if live is None:
                closed = True
                false_close = case != 'd'                 # only case d may end in a close
                restored_s = now - t0 if case == 'd' else restored_s
                break
            if sl_ok:
                if sl_absent_since is not None:
                    sl_absent_max = max(sl_absent_max, now - sl_absent_since)
                    sl_absent_since = None
            elif sl_absent_since is None:
                sl_absent_since = now
            if case != 'd' and sl_ok and tp_ok and (case != 'e' or db.row(row['id'])['status'] == 'open'):
                restored_s = now - t0
                break
            await clock.sleep(POLL_S)
        if sl_absent_since is not None:
            sl_absent_max = max(sl_absent_max, clock.time() - sl_absent_since)
    finally:
        cleanup(ex, symbol, [sl_oid, tp_oid] + sorted(guardian._retired.get((user['id'], market['symbol']), set())))
    ok = (restored_s is not None and restored_s <= args.max_wait and sl_absent_max <= args.max_wait
          and not false_close and (closed if case == 'd' else not closed))
    return {'case': case, 'run': run, 'restored_s': None if restored_s is None else round(restored_s, 1),
            'sl_absent_max_s': round(sl_absent_max, 1), 'closed': closed, 'false_close': false_close,
            'pass': ok}


def cleanup(ex, symbol, oids):
    for oid in oids:
        try:
            ex.cancel_order(oid, symbol)
        except Exception:
            pass
    try:
        pos, _ = book(ex, symbol)
        if pos is not None:
            side = 'sell' if str(pos.get('side')).lower() == 'long' else 'buy'
            ex.create_order(symbol, 'market', side, abs(float(pos['contracts'])), None, {'reduceOnly': True})
    except Exception as e:
        print('CLEANUP WARNING: %s - check the demo account by hand' % type(e).__name__)


async def main_async(args):
    os.environ['GUARDIAN_MODE'] = 'enforce'
    import position_guardian as pg

    async def printed(title, details, level='warning'):
        print('NOTIFY[%s] %s %s' % (level, title, json.dumps(details, default=str)[:300]))
        return True

    pg.notify_admin = printed
    clock = Clock(args.fake)
    ex = make_exchange(args, clock)
    out_dir = os.path.join(ROOT, 'chaos_out', datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    os.makedirs(out_dir, exist_ok=True)
    rows, shapes = [], {}
    for case in args.cases.split(','):
        for run in range(1, args.runs + 1):
            result = await one_run(case, run, args, ex, clock, shapes)
            rows.append(result)
            print('case=%(case)s run=%(run)d restored_s=%(restored_s)s sl_absent_max_s=%(sl_absent_max_s)s '
                  'closed=%(closed)s false_close=%(false_close)s pass=%(pass)s' % result)
    with open(os.path.join(out_dir, 'runs.csv'), 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    summary = {case: {'runs': len([r for r in rows if r['case'] == case]),
                      'passed': len([r for r in rows if r['case'] == case and r['pass']]),
                      'max_restored_s': max([r['restored_s'] or 0 for r in rows if r['case'] == case]),
                      'max_sl_absent_s': max([r['sl_absent_max_s'] for r in rows if r['case'] == case]),
                      'false_closes': len([r for r in rows if r['case'] == case and r['false_close']])}
               for case in args.cases.split(',')}
    summary['_meta'] = {'fake': args.fake, 'guardian_period_s': args.guardian_period, 'max_wait_s': args.max_wait,
                        'symbol': args.symbol}
    with open(os.path.join(out_dir, 'summary.json'), 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2)
    with open(os.path.join(out_dir, 'shapes.json'), 'w', encoding='utf-8') as f:
        json.dump(shapes, f, indent=2, default=str)
    all_pass = all(r['pass'] for r in rows)
    print('CHAOS %s -> %s' % ('PASS' if all_pass else 'FAIL', out_dir))
    return 0 if all_pass else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--cases', default='a,b,c,d,e')
    parser.add_argument('--runs', type=int, default=20)
    parser.add_argument('--guardian-period', type=float, default=120.0)
    parser.add_argument('--max-wait', type=float, default=330.0)
    parser.add_argument('--symbol', default=DEFAULT_SYMBOL)
    parser.add_argument('--fake', action='store_true', help='offline self-test against the test fake (no network)')
    parser.add_argument('--i-accept-demo-orders', action='store_true')
    args = parser.parse_args(argv)
    bad = refusals(args, os.environ)
    if bad:
        for reason in bad:
            print('REFUSED: %s' % reason)
        return 2
    return asyncio.run(main_async(args))


if __name__ == '__main__':
    sys.exit(main())

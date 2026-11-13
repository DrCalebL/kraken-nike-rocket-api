"""hold_guard: validation and sizing of the BNB hold that Kraken followers copy from Binance (T1LIVE-7 follower side).

Pure: stdlib only, no DB and no exchange calls. Used by POST /api/broadcast-hold (validate_hold_payload) and by
hosted_trading_loop.execute_hold_entry (size_hold). Kraken-only; it is NOT one of the sha-shared modules.

Contract with the master (protocol 'hold-1'): {"master_trade_id": hex32, "leader_trade_id": str, "action": "BUY",
"symbol": "BNB/USDT", "ref_entry": str, "stop_loss": str, "r_b": str, "n2": str, "leader_entry_ms": int,
"offer_ms": int, "offer_kind": "entry"|"reoffer", "protocol": "hold-1"}. The same master_trade_id is re-sent daily
while Binance's hold is open; one offer is identified by (master_trade_id, offer_ms).

Sizing (the HL vault ruling "Binance R", ported from rocket_twin/leader.py g1c_size at the FOLLOWER's own mark P):
    q = min(eq * r_b / (P - stop), eq * n2 / P) * HOLD_SIZE_SCALE, floored to the lot.
The follower risks r_b of ITS equity if the shared stop is hit, and never more than the normal n2 notional (never
sizes up on a better price). A late join sizes at its own price (owner: same as the HL vault G1c); the only price
refusal is P <= stop * 1.01. Leverage is fixed at 10x and
is NEVER raised: when the margin does not fit the free margin the size shrinks to fit, and below one lot the entry is
skipped (HOLD_MARGIN_SKIP). Every unreadable or non-finite input refuses (fail closed).
"""
import math
import os
import re
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal, InvalidOperation

HOLD_PROTOCOL = 'hold-1'
HOLD_ACTION = 'BUY'
HOLD_SYMBOL = 'BNB/USDT'
OFFER_KINDS = ('entry', 'reoffer')
HOLD_LEVERAGE = 10                      # fixed; never raised
HOLD_MAX_AGE_MS = 15 * 60_000           # an offer older than this is refused (endpoint) / not executed (loop)
HOLD_MAX_FUTURE_MS = 5 * 60_000
MIN_STOP_DISTANCE = Decimal('1.01')     # P must be above stop * 1.01
DEFAULT_MAX_RISK_PCT = Decimal('0.15')
MARGIN_FACTOR = Decimal('1.5')          # Kraken: (position value / leverage) x 1.5 must fit the available margin
MARGIN_USE = Decimal('0.95')            # use at most 95% of the free margin

# size_hold reasons
HOLD_OK = 'HOLD_OK'
HOLD_BAD_INPUT = 'HOLD_BAD_INPUT'
HOLD_STOP_NOT_BELOW_REF = 'HOLD_STOP_NOT_BELOW_REF'
HOLD_MARK_NEAR_STOP = 'HOLD_MARK_NEAR_STOP'
HOLD_SIZE_ZERO = 'HOLD_SIZE_ZERO'
HOLD_MARGIN_SKIP = 'HOLD_MARGIN_SKIP'
HOLD_MARGIN_UNKNOWN = 'HOLD_MARGIN_UNKNOWN'

_TRADE_ID_RE = re.compile(r'[0-9a-f]{32}')


def _dec(v):
    """Finite Decimal from a str / int / float (never bool), else None."""
    if isinstance(v, bool) or v is None:
        return None
    try:
        d = Decimal(str(v).strip()) if isinstance(v, str) else Decimal(repr(v)) if isinstance(v, float) else Decimal(v)
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def _pos(v):
    d = _dec(v)
    return d if d is not None and d > 0 else None


def _strict_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def max_risk_pct():
    """HOLD_MAX_RISK_PCT (default 0.15). An unreadable or non-positive value falls back to the default."""
    d = _pos(os.getenv('HOLD_MAX_RISK_PCT'))
    return d if d is not None else DEFAULT_MAX_RISK_PCT


def size_scale():
    """HOLD_SIZE_SCALE (default 1.0), capped at 1.0 (never sizes up). A set but unreadable or non-positive value
    returns 0 (fail closed: the size floors to zero and the entry is skipped)."""
    raw = os.getenv('HOLD_SIZE_SCALE')
    if raw is None or not raw.strip():
        return Decimal(1)
    d = _pos(raw)
    if d is None:
        return Decimal(0)
    return min(d, Decimal(1))


def is_master_trade_id(v):
    return isinstance(v, str) and _TRADE_ID_RE.fullmatch(v) is not None


def validate_hold_payload(d, known_symbols, now_ms, max_risk=None):
    """None when the broadcast-hold payload is acceptable, else the 422 reason (it starts with the failing field).
    protocol 'hold-1'; action 'BUY' only; master_trade_id 32 lowercase hex; leader_trade_id a non-empty str (<= 64);
    symbol 'BNB/USDT' and in known_symbols; 0 < stop_loss < ref_entry; 0 < r_b <= HOLD_MAX_RISK_PCT (default 0.15);
    0 < n2 <= 1; leader_entry_ms an int > 0 not in the future; offer_ms an int not older than 15 min and not more
    than 5 min ahead; offer_kind entry|reoffer. Never raises, never mutates d."""
    if not isinstance(d, dict):
        return 'payload must be a JSON object'
    if d.get('protocol') != HOLD_PROTOCOL:
        return 'protocol must be %r' % HOLD_PROTOCOL
    if d.get('action') != HOLD_ACTION:
        return 'action must be %s (holds are long only)' % HOLD_ACTION
    if not is_master_trade_id(d.get('master_trade_id')):
        return 'master_trade_id must be 32 lowercase hex characters'
    lt = d.get('leader_trade_id')
    if not isinstance(lt, str) or not lt.strip() or len(lt) > 64:
        return 'leader_trade_id must be a non-empty string of at most 64 characters'
    sym = d.get('symbol')
    if sym != HOLD_SYMBOL:
        return 'symbol must be %s' % HOLD_SYMBOL
    try:
        known = known_symbols is not None and sym in known_symbols
    except TypeError:
        known = False
    if not known:
        return 'symbol is not traded by this service'
    ref, stop = _pos(d.get('ref_entry')), _pos(d.get('stop_loss'))
    if ref is None:
        return 'ref_entry must be a positive finite number'
    if stop is None:
        return 'stop_loss must be a positive finite number'
    if not stop < ref:
        return 'stop_loss must be below ref_entry'
    r_b, n2 = _pos(d.get('r_b')), _pos(d.get('n2'))
    cap = _pos(max_risk) if max_risk is not None else max_risk_pct()
    if r_b is None or cap is None or r_b > cap:
        return 'r_b must be in (0, %s]' % (cap if cap is not None else DEFAULT_MAX_RISK_PCT)
    if n2 is None or n2 > 1:
        return 'n2 must be in (0, 1]'
    le = d.get('leader_entry_ms')
    if not _strict_int(le) or le <= 0 or le > now_ms + HOLD_MAX_FUTURE_MS:
        return 'leader_entry_ms must be a positive integer (epoch milliseconds) not in the future'
    om = d.get('offer_ms')
    if not _strict_int(om):
        return 'offer_ms must be an integer (epoch milliseconds)'
    if om < now_ms - HOLD_MAX_AGE_MS:
        return 'offer_ms is older than 15 minutes'
    if om > now_ms + HOLD_MAX_FUTURE_MS:
        return 'offer_ms is more than 5 minutes in the future'
    if d.get('offer_kind') not in OFFER_KINDS:
        return 'offer_kind must be one of %s' % ', '.join(OFFER_KINDS)
    return None


def offer_is_fresh(offer_ms, now_ms):
    """The loop's re-check before an entry: the offer is at most 15 min old and not from the future."""
    return _strict_int(offer_ms) and now_ms - HOLD_MAX_AGE_MS <= offer_ms <= now_ms + HOLD_MAX_FUTURE_MS


@dataclass
class HoldSize:
    qty: float = None                   # base units (contracts), floored to the lot; None = refused
    reason: str = HOLD_OK
    leverage: int = HOLD_LEVERAGE
    shrunk: bool = False                # the margin did not fit: qty is the largest size that does
    q_wanted: float = None              # the size before the margin shrink
    margin_needed: float = None         # for qty at the fixed leverage, with Kraken's 1.5x factor
    detail: str = ''

    @property
    def ok(self):
        return self.qty is not None and self.reason == HOLD_OK


def _floor_lot(x, lot):
    return (x / lot).to_integral_value(rounding=ROUND_FLOOR) * lot


def size_hold(equity, mark, ref_entry, stop, r_b, n2, lot, free_margin, scale=None):
    """HoldSize for one follower. Every input is a number or numeric string; anything unreadable, non-finite or <= 0
    (free_margin may be 0 but not unknown) refuses with HOLD_BAD_INPUT, free_margin None with HOLD_MARGIN_UNKNOWN."""
    eq, p, ref, st, rb, nn, lt = (_pos(x) for x in (equity, mark, ref_entry, stop, r_b, n2, lot))
    if None in (eq, p, ref, st, rb, nn, lt):
        return HoldSize(reason=HOLD_BAD_INPUT)
    if free_margin is None:
        return HoldSize(reason=HOLD_MARGIN_UNKNOWN)
    free = _dec(free_margin)
    if free is None or free < 0:
        return HoldSize(reason=HOLD_BAD_INPUT)
    sc = size_scale() if scale is None else _dec(scale)
    if sc is None or sc < 0:
        return HoldSize(reason=HOLD_BAD_INPUT)
    sc = min(sc, Decimal(1))
    if st >= ref:
        return HoldSize(reason=HOLD_STOP_NOT_BELOW_REF)
    if p <= st * MIN_STOP_DISTANCE:
        return HoldSize(reason=HOLD_MARK_NEAR_STOP, detail='mark %s <= stop %s x 1.01' % (p, st))
    q_risk = eq * rb / (p - st)
    q_cap = eq * nn / p
    q = _floor_lot(min(q_risk, q_cap) * sc, lt)
    if q <= 0:
        return HoldSize(reason=HOLD_SIZE_ZERO)
    lev = Decimal(HOLD_LEVERAGE)
    q_fit = _floor_lot(free * MARGIN_USE * lev / (MARGIN_FACTOR * p), lt)       # the largest size that fits
    shrunk = False
    q_wanted = q
    if q > q_fit:
        shrunk = True
        q = q_fit
        if q <= 0:
            return HoldSize(reason=HOLD_MARGIN_SKIP, shrunk=True, q_wanted=float(q_wanted),
                            detail='free margin %s fits less than one lot at %dx' % (free, HOLD_LEVERAGE))
    return HoldSize(qty=float(q), reason=HOLD_OK, shrunk=shrunk, q_wanted=float(q_wanted),
                    margin_needed=float(q * p / lev * MARGIN_FACTOR))


ALLOW_ALL = 'ALL'


def parse_allowlist(raw):
    """HOLD_USER_ALLOWLIST: a comma separated list of follower ids (digits) and/or e-mails; '*' = every follower.
    Unset or blank = NOBODY (None). Returns (ids:set[int], emails:set[str]), ALLOW_ALL or None."""
    if raw is None or not str(raw).strip():
        return None
    ids, emails = set(), set()
    for tok in str(raw).split(','):
        tok = tok.strip().lower()
        if not tok:
            continue
        if tok == '*':
            return ALLOW_ALL
        if tok.isdigit():
            ids.add(int(tok))
        else:
            emails.add(tok)
    return ids, emails


def user_allowed(allowlist, user_id, email):
    """True only for '*' or a follower on the list. No list (unset/blank) and a list that parsed to nothing allow nobody."""
    if allowlist is None:
        return False
    if allowlist == ALLOW_ALL:
        return True
    ids, emails = allowlist
    return user_id in ids or str(email or '').strip().lower() in emails


def floor_to(x, lot):
    """Float helper for tests / callers: floor x to a multiple of lot."""
    if not (math.isfinite(x) and math.isfinite(lot) and lot > 0):
        return None
    return float(_floor_lot(Decimal(repr(x)), Decimal(repr(lot))))

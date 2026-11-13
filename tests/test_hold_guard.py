"""
T1LIVE-7 hold_guard: payload validation, G1c sizing at the follower's mark, the fixed 10x leverage with margin shrink,
the allowlist. Pure functions, no fixtures.
"""
import pytest

import hold_guard as hg

NOW = 1_790_000_000_000
MTID = '0123456789abcdef0123456789abcdef'
KNOWN = {'ADA/USDT': 'PF_ADAUSD', 'BNB/USDT': 'PF_BNBUSD'}


def good(**over):
    d = {'master_trade_id': MTID, 'leader_trade_id': 'L-1', 'action': 'BUY', 'symbol': 'BNB/USDT',
         'ref_entry': '600.0', 'stop_loss': '570.0', 'r_b': '0.02', 'n2': '0.5', 'leader_entry_ms': NOW - 3_600_000,
         'offer_ms': NOW - 1000, 'offer_kind': 'entry', 'protocol': 'hold-1'}
    d.update(over)
    return d


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ('HOLD_MAX_RISK_PCT', 'HOLD_SIZE_SCALE'):
        monkeypatch.delenv(k, raising=False)


def test_a_good_payload_passes_and_is_not_mutated():
    d = good()
    before = dict(d)
    assert hg.validate_hold_payload(d, KNOWN, NOW) is None
    assert d == before


@pytest.mark.parametrize('field,value', [
    ('protocol', 'hold-2'), ('protocol', 1), ('action', 'SELL'), ('action', 'buy'),
    ('master_trade_id', MTID.upper()), ('master_trade_id', MTID[:-1]), ('master_trade_id', None),
    ('leader_trade_id', ''), ('leader_trade_id', 7), ('symbol', 'ADA/USDT'), ('symbol', 'BTC/USDT'),
    ('ref_entry', '0'), ('ref_entry', '-1'), ('ref_entry', 'nan'), ('ref_entry', 'inf'), ('ref_entry', None),
    ('ref_entry', True), ('stop_loss', '600'), ('stop_loss', '601'), ('stop_loss', '0'), ('stop_loss', 'x'),
    ('r_b', '0'), ('r_b', '0.1500001'), ('r_b', '-0.01'), ('r_b', 'nan'), ('n2', '0'), ('n2', '1.01'),
    ('n2', None), ('leader_entry_ms', 0), ('leader_entry_ms', NOW + 600_000), ('leader_entry_ms', 1.5e12),
    ('leader_entry_ms', True), ('offer_ms', NOW - 15 * 60_000 - 1), ('offer_ms', NOW + 5 * 60_000 + 1),
    ('offer_ms', str(NOW)), ('offer_ms', None), ('offer_kind', 'ENTRY'), ('offer_kind', 'add'),
])
def test_bad_fields_are_refused_with_the_field_first(field, value):
    reason = hg.validate_hold_payload(good(**{field: value}), KNOWN, NOW)
    assert isinstance(reason, str) and reason.startswith(field)


def test_missing_keys_and_non_objects_are_refused():
    for k in good():
        d = good()
        d.pop(k)
        assert hg.validate_hold_payload(d, KNOWN, NOW)
    for bad in (None, [], 'x', 3):
        assert hg.validate_hold_payload(bad, KNOWN, NOW) == 'payload must be a JSON object'


def test_symbol_must_be_a_known_service_symbol():
    assert hg.validate_hold_payload(good(), {'ADA/USDT': 'x'}, NOW).startswith('symbol')
    assert hg.validate_hold_payload(good(), None, NOW).startswith('symbol')


def test_offer_age_edges_and_reoffer_kind():
    assert hg.validate_hold_payload(good(offer_ms=NOW - 15 * 60_000), KNOWN, NOW) is None
    assert hg.validate_hold_payload(good(offer_kind='reoffer'), KNOWN, NOW) is None
    assert hg.offer_is_fresh(NOW - 900_000, NOW) and not hg.offer_is_fresh(NOW - 900_001, NOW)
    assert not hg.offer_is_fresh(None, NOW) and not hg.offer_is_fresh('1', NOW)


def test_risk_cap_default_015_and_env_override(monkeypatch):
    assert hg.validate_hold_payload(good(r_b='0.15'), KNOWN, NOW) is None
    monkeypatch.setenv('HOLD_MAX_RISK_PCT', '0.05')
    assert hg.validate_hold_payload(good(r_b='0.06'), KNOWN, NOW).startswith('r_b')
    monkeypatch.setenv('HOLD_MAX_RISK_PCT', 'junk')
    assert hg.validate_hold_payload(good(r_b='0.15'), KNOWN, NOW) is None


# ---------------- sizing ----------------

def size(**kw):
    a = dict(equity=10000, mark=600.0, ref_entry=600.0, stop=570.0, r_b=0.02, n2=0.5, lot=0.01, free_margin=10000)
    a.update(kw)
    return hg.size_hold(**a)


def test_risk_sized_when_the_risk_leg_is_smaller():
    s = size()                                  # risk: 10000 * 0.02 / 30 = 6.666 ; cap: 10000 * 0.5 / 600 = 8.33
    assert s.ok and s.qty == 6.66 and s.leverage == 10 and not s.shrunk


def test_notional_cap_when_the_stop_is_tight():
    s = size(stop=560.0, r_b=0.15)              # risk: 37.5 ; cap 8.33
    assert s.ok and s.qty == 8.33


def test_sizes_at_the_followers_mark_not_the_leaders_ref():
    a, b = size(mark=610.0), size(mark=590.0)
    assert a.qty == 5.0                         # risk leg: 200 / (610 - 570)
    assert b.qty == 8.47                        # risk leg 10.0 is above the notional cap 5000 / 590


def test_never_sizes_up_on_a_better_price():
    far = size(mark=588.0)                      # within 3% of ref
    assert far.qty <= 10000 * 0.5 / 588.0 + 1e-9


@pytest.mark.parametrize('mark', [570.0, 575.0, 575.69])
def test_refused_when_the_mark_is_within_one_percent_of_the_stop(mark):
    s = size(mark=mark, ref_entry=575.0)
    assert not s.ok and s.reason == hg.HOLD_MARK_NEAR_STOP and s.qty is None


def test_a_late_join_far_from_the_leaders_ref_is_sized_not_refused():
    s = size(mark=648.0)                        # 8% above ref: sized at its own price (owner: same as HL G1c)
    assert s.ok and s.qty == 2.56               # risk leg 200 / (648 - 570); cap 5000 / 648 = 7.71
    assert size(mark=585.0, ref_entry=600.0).ok # below ref too: only the near-stop refusal stays
    assert not hasattr(hg, 'HOLD_MARK_DEVIATION')


def test_size_scale_halves_and_caps_at_one(monkeypatch):
    assert size(scale='0.5').qty == 3.33
    assert size(scale='3').qty == size().qty
    monkeypatch.setenv('HOLD_SIZE_SCALE', '0.5')
    assert size().qty == 3.33
    monkeypatch.setenv('HOLD_SIZE_SCALE', 'junk')
    assert size().reason == hg.HOLD_SIZE_ZERO                 # a set but unreadable scale fails closed


def test_margin_shrinks_to_fit_and_never_raises_leverage():
    full = size()
    s = size(free_margin=300)                   # 300 * 0.95 * 10 / (1.5 * 600) = 3.166 -> 3.16
    assert s.ok and s.shrunk and s.qty == 3.16 and s.q_wanted == full.qty and s.leverage == 10
    assert s.margin_needed <= 300 * 0.95 + 1e-9


def test_margin_skip_below_one_lot():
    s = size(free_margin=0.5)
    assert s.reason == hg.HOLD_MARGIN_SKIP and s.qty is None and s.shrunk
    assert size(free_margin=0).reason == hg.HOLD_MARGIN_SKIP


def test_unknown_free_margin_fails_closed():
    assert size(free_margin=None).reason == hg.HOLD_MARGIN_UNKNOWN


@pytest.mark.parametrize('field,bad', [('equity', 0), ('equity', -5), ('equity', 'nan'), ('mark', None),
                                       ('ref_entry', 0), ('stop', 'x'), ('r_b', 0), ('n2', float('inf')),
                                       ('lot', 0), ('free_margin', -1), ('free_margin', 'nan')])
def test_bad_inputs_refuse(field, bad):
    s = size(**{field: bad})
    assert not s.ok and s.qty is None


def test_stop_at_or_above_ref_refuses():
    assert size(stop=600.0).reason == hg.HOLD_STOP_NOT_BELOW_REF


def test_a_tiny_account_floors_to_zero():
    assert size(equity=1.0).reason in (hg.HOLD_SIZE_ZERO, hg.HOLD_MARGIN_SKIP)


# ---------------- allowlist ----------------

def test_allowlist_parsing_and_membership():
    assert hg.parse_allowlist(None) is None and hg.parse_allowlist('  ') is None
    assert not hg.user_allowed(None, 5, 'a@b.c')                       # unset / blank = NOBODY
    assert not hg.user_allowed(hg.parse_allowlist(''), 5, 'a@b.c')
    assert hg.user_allowed(hg.parse_allowlist('*'), 5, 'a@b.c')        # '*' = everyone
    assert hg.user_allowed(hg.parse_allowlist('7, *'), 99, 'x')
    al = hg.parse_allowlist('7, A@B.com ,,12')
    assert hg.user_allowed(al, 7, 'x') and hg.user_allowed(al, 99, 'a@b.COM') and not hg.user_allowed(al, 8, 'q@q.q')
    assert not hg.user_allowed(hg.parse_allowlist(','), 1, 'a@b.c')       # a list that parsed to nothing allows nobody

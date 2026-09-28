"""
QC coverage-followers-7: the owner-run chaos tool (tools/chaos_follower.py) - its interlocks refuse
production settings, and its offline --fake self-test completes and passes even when stdout is a
redirected non-UTF-8 stream (Windows cp1252), where emoji alarm titles used to crash it mid-pass.
"""
import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import chaos_follower  # noqa: E402

SCRUB = ('FOLLOWER_API_URL', 'MASTER_API_KEY', 'DATABASE_URL', 'ROCKET_TESTNET', 'CHAOS_KRAKEN_DEMO_KEY',
         'CHAOS_KRAKEN_DEMO_SECRET', 'PYTHONUTF8', 'PYTHONIOENCODING', 'GUARDIAN_MODE')


def _args(**kw):
    base = dict(fake=False, i_accept_demo_orders=False)
    base.update(kw)
    return argparse.Namespace(**base)


def test_interlocks_refuse_production_settings_and_missing_consent():
    env = {'FOLLOWER_API_URL': 'https://x', 'MASTER_API_KEY': 'k', 'DATABASE_URL': 'postgres://x'}
    out = chaos_follower.refusals(_args(fake=True), env)
    assert len(out) == 3 and all('must never reach production' in r for r in out)
    out = chaos_follower.refusals(_args(), {})
    assert any('--i-accept-demo-orders' in r for r in out)
    assert any('ROCKET_TESTNET=1' in r for r in out)
    assert any('CHAOS_KRAKEN_DEMO_KEY' in r for r in out)
    assert chaos_follower.refusals(_args(fake=True), {}) == []
    ok = {'ROCKET_TESTNET': '1', 'CHAOS_KRAKEN_DEMO_KEY': 'a', 'CHAOS_KRAKEN_DEMO_SECRET': 'b'}
    assert chaos_follower.refusals(_args(i_accept_demo_orders=True), ok) == []


def test_fake_self_test_passes_with_redirected_cp1252_stdout(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in SCRUB}
    env['PYTHONIOENCODING'] = 'cp1252'                   # a redirected Windows console stream
    env['PYTHONUTF8'] = '0'
    proc = subprocess.run([sys.executable, os.path.join(ROOT, 'tools', 'chaos_follower.py'), '--fake',
                           '--cases', 'a,b,c,d,e', '--runs', '1', '--out', str(tmp_path)],
                          cwd=ROOT, env=env, capture_output=True, timeout=300)
    out = proc.stdout.decode('cp1252', errors='replace')
    assert proc.returncode == 0, out[-2000:] + proc.stderr.decode('cp1252', errors='replace')[-2000:]
    assert 'CHAOS PASS' in out
    summary_path, = [os.path.join(d, 'summary.json') for d, _s, files in os.walk(tmp_path) if 'summary.json' in files]
    with open(summary_path, encoding='utf-8') as f:
        summary = json.load(f)
    assert all(summary[c]['passed'] == summary[c]['runs'] == 1 for c in 'abcde')

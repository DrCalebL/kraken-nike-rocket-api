#!/usr/bin/env python
"""
Local gate for kraken-nike-rocket-api (there is no GitHub Actions; spec C39 / T19 / AC3).

  pip install -r requirements-gate.txt      # CPython 3.11-3.13; the gate set, not production
  python tools/run_gates.py

1. import-check the gate dependencies; FAIL with the install hint if any is missing;
2. py_compile the Wave 1 modules, the shared modules, tools/*.py, tests/*.py and tests/fakes/*.py;
3. FAIL if any test file uses importorskip; run pytest over the explicit DB-free list with
   --junitxml, then parse the report: FAIL on any failure, error, skip or xfail, or on a listed
   module with 0 tests;
4. sha256 (CRLF->LF normalised): bracket_guard.py + tests/test_bracket_guard.py must equal
   ../massive-rocket-algos; follower_guard.py, signal_guard.py, tests/test_follower_common.py must equal
   ../hyperliquid-nike-rocket-api.
Writes gate_logs/gates_<UTC>.log and gate_logs/junit_<UTC>.xml; prints GATES PASS|FAIL; exit 0|1.
test_billing_integration.py stays excluded (asyncpg + TEST_DATABASE_URL).
"""
import glob
import hashlib
import importlib
import os
import py_compile
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE_MODULES = ('pytest', 'pytest_asyncio', 'pytest_mock', 'fastapi', 'pydantic', 'email_validator',
                'sqlalchemy', 'httpx', 'cryptography', 'aiohttp', 'requests', 'ccxt')
COMPILE = ('hosted_trading_loop.py', 'order_utils.py', 'position_guardian.py', 'follower_endpoints.py',
           'bracket_guard.py', 'follower_guard.py', 'signal_guard.py')
TESTS = ('tests/test_bracket_guard.py', 'tests/test_follower_common.py', 'tests/test_wait_for_position.py',
         'tests/test_bracket_sl_first.py', 'tests/test_position_guardian_invariant.py',
         'tests/test_guardian_row_policy.py', 'tests/test_fail_closed_and_bounds.py',
         'tests/test_endpoint_static.py', 'tests/test_post_entry_recovery.py', 'tests/test_chaos_tool.py')
SHARED = {
    'massive-rocket-algos': ('bracket_guard.py', 'tests/test_bracket_guard.py'),
    'hyperliquid-nike-rocket-api': ('follower_guard.py', 'signal_guard.py', 'tests/test_follower_common.py'),
}


class Gate:
    def __init__(self, log_path):
        self.log_path = log_path
        self.lines = []
        self.failed = False

    def say(self, line):
        print(line)
        self.lines.append(line)

    def fail(self, line):
        self.failed = True
        self.say('FAIL: ' + line)

    def write(self):
        with open(self.log_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(self.lines) + '\n')


def sha256(path):
    """CRLF->LF-normalised sha256: every rocket repo uses core.autocrlf=true, so checkouts may differ only in EOLs."""
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read().replace(b'\r\n', b'\n')).hexdigest()



def check_imports(gate):
    missing = []
    for name in GATE_MODULES:
        try:
            importlib.import_module(name)
        except ImportError:
            missing.append(name)
    if missing:
        gate.fail('missing gate dependencies: %s -> run: python -m pip install -r requirements-gate.txt'
                  % ', '.join(missing))
    return not missing


def compile_all(gate):
    files = [os.path.join(ROOT, f) for f in COMPILE]
    for pattern in ('tools/*.py', 'tests/*.py', 'tests/fakes/*.py'):
        files += sorted(glob.glob(os.path.join(ROOT, pattern)))
    for path in files:
        try:
            py_compile.compile(path, doraise=True)
        except (py_compile.PyCompileError, FileNotFoundError, OSError) as e:
            gate.fail('py_compile %s: %s' % (os.path.relpath(path, ROOT), e))
    gate.say('py_compile: %d files' % len(files))


def no_importorskip(gate):
    needle = 'import' + 'orskip'
    for path in sorted(glob.glob(os.path.join(ROOT, 'tests', '**', '*.py'), recursive=True)):
        with open(path, encoding='utf-8') as f:
            if needle in f.read():
                gate.fail('%s uses %s (tests must run, never skip)' % (os.path.relpath(path, ROOT), needle))


def run_pytest(gate, junit):
    cmd = [sys.executable, '-m', 'pytest', '-q', '-rA', '-p', 'no:cacheprovider', '--junitxml=%s' % junit]
    cmd += list(TESTS)
    env = dict(os.environ, PYTHONUTF8='1')
    proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, encoding='utf-8',
                          errors='replace')
    gate.lines.append(proc.stdout)
    gate.lines.append(proc.stderr)
    tail = [line for line in proc.stdout.splitlines() if line.strip()][-1:] or ['(no pytest output)']
    gate.say('pytest: %s' % tail[0])
    if not os.path.exists(junit):
        gate.fail('pytest wrote no junit report (exit %s)' % proc.returncode)
        return 0, 0, 0, 0
    tests = failures = errors = skipped = 0
    per_module = {}
    for case in ET.parse(junit).getroot().iter('testcase'):
        tests += 1
        module = case.get('classname', '').split('.')
        key = '.'.join(module[:2]) if module[:1] == ['tests'] else case.get('classname', '')
        per_module[key] = per_module.get(key, 0) + 1
        failures += len(case.findall('failure'))
        errors += len(case.findall('error'))
        skipped += len(case.findall('skipped'))
    for path in TESTS:
        key = path[:-3].replace('/', '.')
        if per_module.get(key, 0) == 0:
            gate.fail('%s ran 0 tests' % path)
    if failures or errors or skipped:
        gate.fail('junit: %d failures, %d errors, %d skipped/xfail' % (failures, errors, skipped))
    if proc.returncode != 0 and not (failures or errors):
        gate.fail('pytest exit code %s' % proc.returncode)
    return tests, failures, errors, skipped


def compare_shared(gate):
    for sibling, files in SHARED.items():
        other_root = os.path.normpath(os.path.join(ROOT, '..', sibling))
        for rel in files:
            mine, theirs = os.path.join(ROOT, rel), os.path.join(other_root, rel)
            if not os.path.exists(mine):
                gate.fail('%s is missing' % rel)
                continue
            if not os.path.exists(theirs):
                gate.fail('%s is missing in ../%s (shared files must be byte-identical)' % (rel, sibling))
                continue
            a, b = sha256(mine), sha256(theirs)
            if a != b:
                gate.fail('%s differs from ../%s/%s' % (rel, sibling, rel))
            else:
                gate.say('sha256 %s %s == ../%s' % (a[:16], rel, sibling))


def main():
    os.makedirs(os.path.join(ROOT, 'gate_logs'), exist_ok=True)
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    gate = Gate(os.path.join(ROOT, 'gate_logs', 'gates_%s.log' % stamp))
    junit = os.path.join(ROOT, 'gate_logs', 'junit_%s.xml' % stamp)
    counts = (0, 0, 0, 0)
    if check_imports(gate):
        compile_all(gate)
        no_importorskip(gate)
        counts = run_pytest(gate, junit)
    compare_shared(gate)
    gate.say('GATES %s n_tests=%d failures=%d errors=%d skipped=%d'
             % ((('FAIL' if gate.failed else 'PASS'),) + tuple(counts)))
    gate.write()
    return 1 if gate.failed else 0


if __name__ == '__main__':
    sys.exit(main())

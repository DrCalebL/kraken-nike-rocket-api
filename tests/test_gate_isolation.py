"""
QC security-6: the follower test process can never reach the network (loopback only) and never sees
production credentials, whatever shell runs the gate (tests/conftest.py; tools/run_gates.py scrubs the
child environment too).
"""
import os
import socket

import pytest

from tests.conftest import SCRUBBED_ENV


def test_non_loopback_lookups_and_connections_are_refused():
    with pytest.raises(OSError, match='network blocked in tests'):
        socket.getaddrinfo('api.resend.com', 443)
    with pytest.raises(OSError, match='network blocked in tests'):
        socket.create_connection(('203.0.113.7', 443), timeout=0.5)


def test_loopback_stays_usable():
    a, b = socket.socketpair()
    try:
        a.sendall(b'ok')
        assert b.recv(2) == b'ok'
    finally:
        a.close()
        b.close()


def test_production_credentials_are_not_in_the_test_environment():
    assert {'DATABASE_URL', 'RESEND_API_KEY', 'MASTER_API_KEY', 'ADMIN_PASSWORD',
            'CREDENTIALS_ENCRYPTION_KEY'} <= set(SCRUBBED_ENV)
    assert [name for name in SCRUBBED_ENV if os.environ.get(name)] == []

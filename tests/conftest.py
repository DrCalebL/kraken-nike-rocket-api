"""
QC security-6: process-wide isolation for every follower test, installed before any test module imports.

- The network is blocked for the whole test process: every DNS lookup and connection to a host other than
  loopback raises NetworkBlocked (loopback stays usable: asyncio's socketpair, a local test database).
- Production credentials are removed from the environment, so code under test can never e-mail the admin
  through Resend, open DATABASE_URL, or use a real key, even when the gate runs in a shell that carries them
  (e.g. under `railway run`). tools/run_gates.py also scrubs them from the pytest child environment.
"""
import os
import socket

SCRUBBED_ENV = ('DATABASE_URL', 'RESEND_API_KEY', 'MASTER_API_KEY', 'ADMIN_PASSWORD', 'CREDENTIALS_ENCRYPTION_KEY',
                'FOLLOWER_API_URL', 'COINBASE_WEBHOOK_SECRET', 'COINBASE_COMMERCE_API_KEY')
_LOOPBACK = frozenset({'127.0.0.1', '::1', 'localhost', '0.0.0.0', ''})

for _name in SCRUBBED_ENV:
    os.environ.pop(_name, None)


class NetworkBlocked(OSError):
    """A test tried to reach a non-loopback host."""


def _host_of(address):
    if isinstance(address, (tuple, list)) and address:
        return str(address[0])
    return None


def _block_network():
    """Patch the socket entry points once per process; loopback stays usable."""
    if getattr(socket, '_rocket_blocked', False):
        return
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex
    real_create, real_gai = socket.create_connection, socket.getaddrinfo

    def guard(host, what):
        if host is not None and host not in _LOOPBACK:
            raise NetworkBlocked('network blocked in tests: %s %s' % (what, host))

    def connect(sock, address):
        guard(_host_of(address), 'connect')
        return real_connect(sock, address)

    def connect_ex(sock, address):
        guard(_host_of(address), 'connect_ex')
        return real_connect_ex(sock, address)

    def create_connection(address, *args, **kwargs):
        guard(_host_of(address), 'create_connection')
        return real_create(address, *args, **kwargs)

    def getaddrinfo(host, *args, **kwargs):
        guard(None if host is None else str(host), 'getaddrinfo')
        return real_gai(host, *args, **kwargs)

    socket.socket.connect, socket.socket.connect_ex = connect, connect_ex
    socket.create_connection, socket.getaddrinfo = create_connection, getaddrinfo
    socket._rocket_blocked = True


_block_network()

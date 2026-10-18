"""CI-only outbound network guard (loaded via PYTHONPATH=deploy/autonomous/ci).

Inert unless ``SURGE_BLOCK_NETWORK=1``. When enabled, every non-loopback INET/INET6
``connect``/``connect_ex``/``sendto`` and every non-local DNS lookup raises
``NetworkBlockedError`` and is appended to ``SURGE_NETWORK_VIOLATIONS_LOG`` (one JSON
line per attempt). Code under test may swallow the exception (fail-safe HOLD paths
do exactly that), so the CI job also fails when the violations log is non-empty.

Loopback and AF_UNIX stay allowed: in-process test servers and the authenticated
local control socket are not external network calls. No source file is modified.
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import sys
import traceback

_ENABLED = os.environ.get("SURGE_BLOCK_NETWORK") == "1"
_LOG = os.environ.get("SURGE_NETWORK_VIOLATIONS_LOG", "")
_LOCAL_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback", ""}


class NetworkBlockedError(OSError):
    """Raised for an outbound network attempt while the CI guard is active."""


def _is_local_host(host) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    host = str(host).strip("[]").split("%", 1)[0].lower()
    if host in _LOCAL_NAMES:
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return addr.is_loopback or addr.is_unspecified


def _record(kind: str, target) -> None:
    if _LOG:
        frames = [f for f in traceback.format_stack()[:-2]
                  if "site-packages" not in f and "/lib/python3" not in f]
        entry = {"kind": kind, "target": repr(target), "argv": sys.argv[:3], "stack": frames[-8:]}
        try:
            with open(_LOG, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        except OSError:
            pass
    print(f"SURGE_BLOCK_NETWORK: blocked {kind} to {target!r}", file=sys.stderr)


def _check_address(sock, address, kind: str) -> None:
    if sock.family not in (socket.AF_INET, socket.AF_INET6):
        return
    host = address[0] if isinstance(address, tuple) and address else address
    if not _is_local_host(host):
        _record(kind, address)
        raise NetworkBlockedError(f"outbound network blocked in CI: {kind} {address!r}")


def _install() -> None:
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_sendto = socket.socket.sendto
    real_getaddrinfo = socket.getaddrinfo
    real_create_connection = socket.create_connection

    def connect(self, address):
        _check_address(self, address, "connect")
        return real_connect(self, address)

    def connect_ex(self, address):
        _check_address(self, address, "connect_ex")
        return real_connect_ex(self, address)

    def sendto(self, data, *args):
        _check_address(self, args[-1], "sendto")
        return real_sendto(self, data, *args)

    def getaddrinfo(host, *args, **kwargs):
        if not _is_local_host(host):
            _record("getaddrinfo", host)
            raise NetworkBlockedError(f"DNS lookup blocked in CI: {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    def create_connection(address, *args, **kwargs):
        if not _is_local_host(address[0]):
            _record("create_connection", address)
            raise NetworkBlockedError(f"outbound network blocked in CI: create_connection {address!r}")
        return real_create_connection(address, *args, **kwargs)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.socket.sendto = sendto
    socket.getaddrinfo = getaddrinfo
    socket.create_connection = create_connection


if _ENABLED:
    _install()

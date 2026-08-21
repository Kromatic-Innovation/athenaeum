# SPDX-License-Identifier: Apache-2.0
"""Pin the athenaeum#1091 invariant: the default pytest selection stays
offline.

Covers ``tests/conftest.py``'s ``_block_non_local_network`` autouse fixture
directly — it blocks a non-local destination and it allows loopback — so the
guard that pins ``pyproject.toml``'s "no outbound network in the default
selection" invariant is itself tested, not just asserted in a docstring.
"""

from __future__ import annotations

import socket
import threading

import pytest

from tests.conftest import NetworkBlockedInDefaultSuite


def test_guard_blocks_non_local_connect() -> None:
    """A connect attempt to a public, non-local address is rejected by the
    autouse guard before any real socket I/O happens.

    203.0.113.0/24 is TEST-NET-3 (RFC 5737) — reserved for documentation, so
    this can never accidentally succeed against a real host even if the
    guard failed to engage.
    """
    with pytest.raises(NetworkBlockedInDefaultSuite, match=r"203\.0\.113\.1"):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.connect(("203.0.113.1", 80))
        finally:
            s.close()


def test_guard_blocks_non_local_create_connection() -> None:
    """Same guard, via the ``socket.create_connection`` entry point (the one
    httpx/httpcore's sync transport normally uses)."""
    with pytest.raises(NetworkBlockedInDefaultSuite, match=r"203\.0\.113\.1"):
        socket.create_connection(("203.0.113.1", 80), timeout=1)


def test_guard_allows_loopback() -> None:
    """A real loopback connection still works — the guard only blocks
    non-local destinations, it does not disable networking entirely."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    accepted: list[socket.socket] = []

    def _accept() -> None:
        conn, _addr = server.accept()
        accepted.append(conn)

    t = threading.Thread(target=_accept, daemon=True)
    t.start()
    try:
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(5)
        client.connect(("127.0.0.1", port))  # must NOT raise
        client.close()
    finally:
        t.join(timeout=5)
        for conn in accepted:
            conn.close()
        server.close()


def test_guard_allows_localhost_hostname() -> None:
    """The ``"localhost"`` literal (not just ``127.0.0.1``) is treated as
    local — ``connect_ex`` variant, since some callers use the non-raising
    error-code form."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(5)
        # connect_ex returns an errno instead of raising; a non-zero result
        # here would come from the OS-level connect, not the guard — the
        # guard would instead raise NetworkBlockedInDefaultSuite, which this
        # call must NOT do for "localhost".
        result = client.connect_ex(("localhost", port))
        client.close()
        assert result == 0
    finally:
        server.close()

# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#1953 -- guard the mcp SDK API cli_tool_bridge.py depends on.

``src/athenaeum/cli_tool_bridge.py`` imports ``mcp.server.lowlevel.Server``
directly and registers handlers with the pre-2.0 decorator API
(``@server.list_tools()`` / ``@server.call_tool()``). That API does not exist
on the ``mcp`` 2.x SDK. ``pyproject.toml`` now pins ``mcp>=1.24,<2.0``
alongside a tightened ``fastmcp`` upper bound (see the comment there for why
both are needed), but a pin is only as good as a test that fails loudly the
moment an install drifts off it -- which is exactly how this surfaced: CI
resolved ``fastmcp-slim==3.4.5`` / ``mcp==1.x`` and passed, while a separate
install resolved ``fastmcp==4.0.8`` / ``mcp==2.2.0`` and crashed with
``AttributeError: 'Server' object has no attribute 'list_tools'`` the first
time the bridge child ran -- four hours into an eval, not at test time.

This test imports the real, installed ``mcp`` package and checks the
decorator API directly, so it fails on the SAME installed environment the
bridge itself would crash in -- no mocking, no fixture.

When this starts failing because the installed ``mcp`` has genuinely moved
to 2.x (e.g. the ``mcp>=1.24,<2.0`` pin was intentionally widened), the fix
is to port ``cli_tool_bridge.py`` to the 2.x registration API -- tracked in
athenaeum#1954 -- and then update or retire this test to match, not to
loosen this assertion in place.
"""

from __future__ import annotations

import importlib.metadata


def test_mcp_server_has_pre_2x_decorator_api() -> None:
    """``cli_tool_bridge.py`` needs ``Server.list_tools``/``Server.call_tool``.

    These are the pre-2.0 ``mcp`` SDK's registration decorators (see the
    real usage in ``src/athenaeum/cli_tool_bridge.py``, which imports
    ``from mcp.server.lowlevel import Server`` and calls
    ``@server.list_tools()`` / ``@server.call_tool()``). The ``mcp`` 2.x SDK
    replaced this pattern, so their absence here means the installed `mcp`
    has drifted onto 2.x despite the `mcp>=1.24,<2.0` pin in pyproject.toml
    -- a resolver/lockfile problem, not a code bug in this test.
    """
    from mcp.server.lowlevel import Server

    installed_version = importlib.metadata.version("mcp")
    missing = [attr for attr in ("list_tools", "call_tool") if not hasattr(Server, attr)]
    assert not missing, (
        f"mcp=={installed_version} is installed, but mcp.server.lowlevel."
        f"Server is missing {missing!r} -- these are the pre-2.0 decorator "
        "registration methods athenaeum.cli_tool_bridge depends on "
        "directly. pyproject.toml pins `mcp>=1.24,<2.0` (athenaeum#1953) "
        "precisely to keep installs on an mcp release that has this API; "
        "this failure means something resolved outside that pin. Do NOT "
        "silence this by relaxing the assertion -- either fix the "
        "resolution to land back inside the pin, or port "
        "cli_tool_bridge.py to the mcp 2.x API (tracked in athenaeum#1954) "
        "and update this test to match the new surface."
    )

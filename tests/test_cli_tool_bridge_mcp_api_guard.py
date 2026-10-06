# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#1954 -- guard the mcp SDK API cli_tool_bridge.py depends on.

``src/athenaeum/cli_tool_bridge.py`` imports ``mcp.server.lowlevel.Server``
directly and registers its two handlers through the ``mcp`` 2.x
``on_list_tools`` / ``on_call_tool`` **constructor callbacks**. The pre-2.0
SDK instead exposed ``@server.list_tools()`` / ``@server.call_tool()``
decorator methods, which 2.x removed.

athenaeum#1953 originally pinned ``mcp>=1.24,<2.0`` and asserted the
*pre-2.0* decorators were present. athenaeum#1954 ported the bridge and
lifted that ceiling to a ``mcp>=2.0,<3.0`` floor, so this guard is inverted:
it now asserts the **2.x** surface. The failure mode is inverted with it --
the risk is no longer "an install drifted forward onto 2.x" but "an install
drifted BACK onto 1.x" (a resolver that picked ``fastmcp`` 3.x, which
declares ``mcp<2.0``), where ``Server.__init__`` would reject the two
keyword arguments the bridge passes.

Why the guard still earns its place rather than being retired: the pre-2.0
API was *also* removed silently, and the original incident surfaced four
hours into an eval rather than at test time, because nothing asserted the
installed surface. That asymmetry is unchanged by the port -- a mismatched
``mcp`` still fails only when the bridge child first runs, in a subprocess
whose traceback the harness reports as a dead tool call. This test imports
the real, installed ``mcp`` package, so it fails on the SAME installed
environment the bridge itself would crash in -- no mocking, no fixture.

If this starts failing because the installed ``mcp`` has genuinely moved to
a 3.x major, the fix is to port the bridge to whatever registration surface
that major exposes and update this test to match -- not to loosen the
assertions in place.
"""

from __future__ import annotations

import importlib.metadata
import inspect

from packaging.version import Version

#: The exact keyword arguments ``cli_tool_bridge.main`` passes to
#: ``Server(...)``. Keep in step with the real call site.
_REQUIRED_SERVER_KWARGS = ("on_list_tools", "on_call_tool")

#: The result models the bridge's two handlers construct and return.
_REQUIRED_TYPES = ("ListToolsResult", "CallToolResult", "Tool", "TextContent")


def test_installed_mcp_is_2x() -> None:
    """The declared floor is ``mcp>=2.0,<3.0`` -- assert the install honours it."""
    installed = Version(importlib.metadata.version("mcp"))
    assert Version("2.0") <= installed < Version("3.0"), (
        f"mcp=={installed} is installed, but athenaeum.cli_tool_bridge targets "
        "the mcp 2.x registration API and pyproject.toml declares "
        "`mcp>=2.0,<3.0` (athenaeum#1954). An install below 2.0 means "
        "something resolved outside that floor -- most likely `fastmcp` 3.x, "
        "which declares `mcp<2.0`. Fix the resolution rather than this "
        "assertion."
    )


def test_mcp_server_accepts_the_2x_handler_callbacks() -> None:
    """``cli_tool_bridge.py`` builds its ``Server`` with ``on_list_tools`` /
    ``on_call_tool`` keyword arguments (the mcp 2.x registration surface).

    Checked against the real ``Server.__init__`` signature, which is what
    would raise ``TypeError`` at bridge start-up if the installed SDK did not
    accept them.
    """
    from mcp.server.lowlevel import Server

    installed_version = importlib.metadata.version("mcp")
    params = inspect.signature(Server.__init__).parameters
    missing = [kw for kw in _REQUIRED_SERVER_KWARGS if kw not in params]
    assert not missing, (
        f"mcp=={installed_version} is installed, but mcp.server.lowlevel."
        f"Server.__init__ does not accept {missing!r} -- these are the 2.x "
        "constructor callbacks athenaeum.cli_tool_bridge registers its "
        "`tools/list` and `tools/call` handlers through (athenaeum#1954). "
        "Do NOT silence this by relaxing the assertion -- either fix the "
        "resolution to land back inside `mcp>=2.0,<3.0`, or port "
        "cli_tool_bridge.py to the registration surface this `mcp` does "
        "expose and update this test to match."
    )
    # They must be keyword-passable, which is how the bridge passes them.
    for kw in _REQUIRED_SERVER_KWARGS:
        assert params[kw].kind is inspect.Parameter.KEYWORD_ONLY, (
            f"mcp=={installed_version}: Server.__init__'s {kw!r} is "
            f"{params[kw].kind!s}, not keyword-only -- cli_tool_bridge passes "
            "it by keyword."
        )


def test_retired_pre_2x_decorator_api_is_absent() -> None:
    """The positive half of the version assertion, read off the API itself.

    ``Server.list_tools`` / ``Server.call_tool`` were the pre-2.0 decorator
    registration methods. Their presence would mean the installed SDK is a
    1.x release regardless of what the metadata version claims (a vendored
    or patched install), and the bridge's constructor-callback registration
    would not be reached.
    """
    from mcp.server.lowlevel import Server

    installed_version = importlib.metadata.version("mcp")
    present = [attr for attr in ("list_tools", "call_tool") if hasattr(Server, attr)]
    assert not present, (
        f"mcp=={installed_version} exposes the retired pre-2.0 decorator "
        f"methods {present!r} on mcp.server.lowlevel.Server. athenaeum#1954 "
        "ported athenaeum.cli_tool_bridge OFF that API onto the 2.x "
        "`on_list_tools`/`on_call_tool` constructor callbacks, so an install "
        "that still has the decorators is a 1.x SDK the ported bridge cannot "
        "run against."
    )


def test_bridge_result_models_exist_on_installed_mcp() -> None:
    """The handlers return real ``mcp.types`` models, not bare lists (the
    pre-2.0 shape). Assert each one the bridge constructs is importable."""
    import mcp.types as types

    installed_version = importlib.metadata.version("mcp")
    missing = [name for name in _REQUIRED_TYPES if not hasattr(types, name)]
    assert not missing, (
        f"mcp=={installed_version} is installed, but mcp.types is missing "
        f"{missing!r} -- athenaeum.cli_tool_bridge's `tools/list` and "
        "`tools/call` handlers construct these models directly "
        "(athenaeum#1954)."
    )

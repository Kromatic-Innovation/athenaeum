# SPDX-License-Identifier: Apache-2.0
"""Packaged static asset for ``athenaeum viewer`` (issue athenaeum#1480).

Data only — no Python logic lives here. ``index.html`` is the single static
page the viewer's HTTP server returns for ``GET /``; it fetches its data from
the server's own ``/data.json`` endpoint at load time via vanilla JS
(``fetch``), so no server-side templating is needed and no new runtime
dependency is introduced.

Loaded by :mod:`athenaeum._cmd_viewer` via ``importlib.resources`` — the same
packaged-data-file pattern ``src/athenaeum/retention_packs/`` and
``src/athenaeum/schema/`` already use. No separate ``pyproject.toml`` entry
is needed: this directory lives under ``src/athenaeum`` (``packages =
["src/athenaeum"]``), which hatchling's default wheel packaging already
includes in full — the explicit ``include = [...]`` list in ``pyproject.toml``
documents that default for a few resource directories, it does not gate it.
"""

# SPDX-License-Identifier: Apache-2.0
"""Central logging config + per-run correlation id (issue #540, M25)."""

from __future__ import annotations

import logging

from athenaeum import logconf


def test_new_run_id_sets_contextvar_and_is_unique() -> None:
    a = logconf.new_run_id()
    assert logconf.run_id_var.get() == a
    assert len(a) == 8
    b = logconf.new_run_id()
    assert b != a
    assert logconf.run_id_var.get() == b


def test_run_id_filter_stamps_record() -> None:
    logconf.new_run_id()
    rid = logconf.run_id_var.get()
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "hi", None, None)
    assert logconf._RunIdFilter().filter(rec) is True
    assert rec.run_id == rid


def test_configure_logging_installs_iso_name_runid_format() -> None:
    root = logging.getLogger()
    saved = root.handlers[:]
    saved_level = root.level
    try:
        logconf.new_run_id()
        rid = logconf.run_id_var.get()
        logconf.configure_logging(verbose=True, force=True)
        assert root.level == logging.DEBUG
        assert root.handlers, "configure_logging installed no handler"
        handler = root.handlers[0]
        fmt = handler.formatter
        assert fmt is not None
        # The format carries logger name, an ISO date (not just H:M:S), and the
        # run id — the three things the old format lacked.
        assert "%(name)s" in fmt._fmt
        assert "%(run_id)s" in fmt._fmt
        assert fmt.datefmt == "%Y-%m-%dT%H:%M:%S"
        # A record routed through the handler's filter gets the run id, and the
        # rendered line contains both the module name and that id.
        rec = logging.LogRecord(
            "athenaeum.demo", logging.INFO, __file__, 1, "hello", None, None
        )
        for f in handler.filters:
            f.filter(rec)
        rendered = fmt.format(rec)
        assert "athenaeum.demo" in rendered
        assert rid in rendered
        assert "hello" in rendered
    finally:
        root.handlers[:] = saved
        root.setLevel(saved_level)


def test_configure_logging_default_is_noop_when_root_has_handlers() -> None:
    # force=False must NOT disturb an already-configured root (e.g. pytest's
    # caplog handler) — this is what keeps caplog working under the CLI cmds.
    root = logging.getLogger()
    saved = root.handlers[:]
    try:
        sentinel = logging.NullHandler()
        root.handlers[:] = [sentinel]
        logconf.configure_logging(verbose=False)  # force defaults to False
        assert root.handlers == [sentinel]
    finally:
        root.handlers[:] = saved

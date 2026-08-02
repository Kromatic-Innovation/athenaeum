# SPDX-License-Identifier: Apache-2.0
"""Durability of the raw-intake write chokepoint (issue athenaeum#534, M13).

The value of moving ``remember()``'s intake write behind
:func:`athenaeum.storage.write_raw_intake` is entirely in the *interrupt* path:
a crash or SIGTERM mid-write must never leave a truncated intake file for the
next compile run to parse as valid. These are interruption tests, not
round-trip tests — the success path is asserted elsewhere (test_mcp_server).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum import atomic_io
from athenaeum.storage import write_raw_intake


def test_write_raw_intake_is_atomic_on_success(tmp_path: Path) -> None:
    target = tmp_path / "raw" / "sessions"
    path = write_raw_intake(target, "full intake body")
    assert path.parent == target
    assert path.read_text(encoding="utf-8") == "full intake body"
    # A UTC-timestamp + short-id .md name, and no temp file left behind.
    assert path.suffix == ".md"
    assert not list(target.glob("*.tmp"))


def test_interrupted_write_leaves_no_torn_intake_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulate a crash after the temp file is written but BEFORE the atomic
    # rename lands. The target path must NOT exist (no torn file the compiler
    # could parse), and the temp file must be cleaned up — never a half-written
    # intake left behind.
    target = tmp_path / "raw" / "sessions"

    def _boom(*_a: object, **_k: object) -> None:
        raise OSError("simulated crash before rename")

    monkeypatch.setattr(atomic_io.os, "replace", _boom)

    with pytest.raises(OSError, match="simulated crash before rename"):
        write_raw_intake(target, "content that must not survive half-written")

    # No intake file at the target, and no leftover temp partial.
    assert not list(target.glob("*.md"))
    assert not list(target.glob("*.tmp"))


def test_interrupted_write_preserves_prior_file_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # atomic_write_text targets a fresh minted filename each call, so an
    # existing file is never the same path; but prove the general guarantee on
    # the primitive directly: a failed rewrite leaves the old content intact.
    existing = tmp_path / "page.md"
    existing.write_text("original durable content", encoding="utf-8")

    def _boom(*_a: object, **_k: object) -> None:
        raise OSError("simulated crash before rename")

    monkeypatch.setattr(atomic_io.os, "replace", _boom)
    with pytest.raises(OSError):
        atomic_io.atomic_write_text(existing, "replacement that must not land")

    assert existing.read_text(encoding="utf-8") == "original durable content"
    assert not list(tmp_path.glob("*.tmp"))

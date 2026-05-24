# SPDX-License-Identifier: Apache-2.0
"""Tests for the expanded resolver-input payload (Lane 2 / issue #168).

Covers ``_build_user_message``'s richer context:

- Small bodies on both sides → full inclusion of both bodies.
- One large + one small → asymmetric truncation; large side shows the
  truncation note, small side shows full body.
- ``[[link]]`` resolution one hop → target description appears.
- ``[[link]]`` to nonexistent target → omitted gracefully.
- Timestamps (``created_at`` / ``updated_at`` / ``originSessionId``)
  appear when present, omitted cleanly when absent.
- Declared ``refines:`` / ``supersedes:`` appear when present.
- Backward compat: ``_build_user_message`` keeps working without the new
  ``config`` argument.
"""

from __future__ import annotations

from pathlib import Path

from athenaeum.contradictions import ContradictionResult
from athenaeum.models import AutoMemoryFile
from athenaeum.resolutions import (
    DEFAULT_FULL_BODY_TOKEN_CAP,
    _build_user_message,
    resolve_full_body_token_cap,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_member(
    scope_dir: Path,
    filename: str,
    body: str,
    *,
    name: str = "probe",
    description: str | None = None,
    source: str | None = None,
    created_at: str | None = None,
    updated_at: str | None = None,
    origin_session_id: str | None = None,
    refines: list[str] | None = None,
    supersedes: list[dict[str, str]] | None = None,
    origin_scope: str = "scope-x",
) -> AutoMemoryFile:
    """Write an auto-memory file and return its AutoMemoryFile record."""
    scope_dir.mkdir(parents=True, exist_ok=True)
    fm: list[str] = ["---", f"name: {name}", "type: feedback"]
    if description is not None:
        fm.append(f"description: {description}")
    if source is not None:
        fm.append(f"source: {source}")
    if created_at is not None:
        fm.append(f"created_at: {created_at}")
    if updated_at is not None:
        fm.append(f"updated_at: {updated_at}")
    if origin_session_id is not None:
        fm.append(f"originSessionId: {origin_session_id}")
    if refines:
        fm.append("refines:")
        for r in refines:
            fm.append(f"  - {r}")
    if supersedes:
        fm.append("supersedes:")
        for s in supersedes:
            fm.append(f"  - name: {s['name']}")
            for k, v in s.items():
                if k == "name":
                    continue
                fm.append(f"    {k}: {v}")
    fm.append("---")
    path = scope_dir / filename
    path.write_text("\n".join(fm) + "\n" + body + "\n", encoding="utf-8")
    return AutoMemoryFile(
        path=path,
        origin_scope=origin_scope,
        memory_type="feedback",
        name=name,
        description=description or "",
        origin_session_id=origin_session_id,
        refines=list(refines or []),
        supersedes=list(supersedes or []),
    )


def _detected(
    members: list[AutoMemoryFile], passages: list[str]
) -> ContradictionResult:
    return ContradictionResult(
        detected=True,
        conflict_type="factual",
        members_involved=[f"{m.origin_scope}/{m.path.name}" for m in members[:2]],
        conflicting_passages=passages,
        rationale="test conflict",
    )


# ---------------------------------------------------------------------------
# Full body inclusion
# ---------------------------------------------------------------------------


def test_small_bodies_both_included(tmp_path: Path) -> None:
    scope = tmp_path / "scope"
    a = _write_member(scope, "a.md", "Member A body content here.")
    b = _write_member(scope, "b.md", "Member B body content here.")
    msg = _build_user_message(
        _detected([a, b], ["passage a", "passage b"]),
        [a, b],
    )
    assert "Member A body content here." in msg
    assert "Member B body content here." in msg
    assert "truncated" not in msg


def test_asymmetric_truncation(tmp_path: Path) -> None:
    scope = tmp_path / "scope"
    a = _write_member(scope, "a.md", "small body")
    # 20 tokens cap × 4 chars = 80 chars char-cap; make b massively over.
    big_body = "X" * 5000
    b = _write_member(scope, "b.md", big_body)
    cfg = {"resolve": {"full_body_token_cap": 20}}
    msg = _build_user_message(
        _detected([a, b], ["passage a", "passage b"]),
        [a, b],
        cfg,
    )
    # Small side: full body included, no truncation note attributed to it.
    assert "small body" in msg
    # Large side: body NOT included, truncation note IS present.
    assert big_body not in msg
    assert "truncated" in msg
    assert "20-token budget" in msg
    # The passage stands in for the body on the truncated side.
    assert "passage b" in msg


# ---------------------------------------------------------------------------
# Wikilink resolution
# ---------------------------------------------------------------------------


def test_one_hop_link_resolution(tmp_path: Path) -> None:
    scope = tmp_path / "scope"
    # Target memory the link points at — sibling in the same scope dir.
    _write_member(
        scope,
        "target.md",
        "Target memory body.",
        name="other-memory",
        description="Other memory short description.",
    )
    a = _write_member(scope, "a.md", "See [[other-memory]] for context.")
    b = _write_member(scope, "b.md", "Unrelated body.")
    msg = _build_user_message(
        _detected([a, b], ["See [[other-memory]] for context.", "passage b"]),
        [a, b],
    )
    assert "link[other-memory]" in msg
    assert "Other memory short description." in msg


def test_link_to_nonexistent_target_is_omitted(tmp_path: Path) -> None:
    scope = tmp_path / "scope"
    a = _write_member(scope, "a.md", "Dangling [[ghost-memory]] reference.")
    b = _write_member(scope, "b.md", "Body b.")
    msg = _build_user_message(
        _detected([a, b], ["Dangling [[ghost-memory]] reference.", "passage b"]),
        [a, b],
    )
    # Body still rendered, but no link[ghost-memory] resolution line.
    assert "ghost-memory" in msg  # appears inside the body
    assert "link[ghost-memory]" not in msg


def test_link_resolution_does_not_recurse(tmp_path: Path) -> None:
    """One-hop only — a link in the target's body must not be followed."""
    scope = tmp_path / "scope"
    _write_member(
        scope,
        "deep.md",
        "Deep [[third-memory]] reference.",
        name="other-memory",
        description="Description of other-memory.",
    )
    _write_member(
        scope,
        "third.md",
        "Third body.",
        name="third-memory",
        description="MUST_NOT_APPEAR description.",
    )
    a = _write_member(scope, "a.md", "See [[other-memory]] here.")
    b = _write_member(scope, "b.md", "Body b.")
    msg = _build_user_message(
        _detected([a, b], ["See [[other-memory]] here.", "passage b"]),
        [a, b],
    )
    assert "Description of other-memory." in msg
    assert "MUST_NOT_APPEAR" not in msg


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


def test_timestamps_appear_when_present(tmp_path: Path) -> None:
    scope = tmp_path / "scope"
    a = _write_member(
        scope,
        "a.md",
        "body a",
        created_at="2026-05-20",
        updated_at="2026-05-22",
        origin_session_id="session-xyz",
    )
    b = _write_member(scope, "b.md", "body b")
    msg = _build_user_message(
        _detected([a, b], ["passage a", "passage b"]),
        [a, b],
    )
    assert "created_at: 2026-05-20" in msg
    assert "updated_at: 2026-05-22" in msg
    assert "originSessionId: session-xyz" in msg


def test_timestamps_omitted_when_absent(tmp_path: Path) -> None:
    scope = tmp_path / "scope"
    a = _write_member(scope, "a.md", "body a")
    b = _write_member(scope, "b.md", "body b")
    msg = _build_user_message(
        _detected([a, b], ["passage a", "passage b"]),
        [a, b],
    )
    # No "None", no empty-value cruft.
    assert "created_at:" not in msg
    assert "updated_at:" not in msg
    assert "originSessionId:" not in msg
    assert "None" not in msg


# ---------------------------------------------------------------------------
# Declared refines / supersedes
# ---------------------------------------------------------------------------


def test_refines_declaration_appears(tmp_path: Path) -> None:
    scope = tmp_path / "scope"
    a = _write_member(scope, "a.md", "body a", refines=["base-rule"])
    b = _write_member(scope, "b.md", "body b")
    msg = _build_user_message(
        _detected([a, b], ["passage a", "passage b"]),
        [a, b],
    )
    assert "refines:" in msg
    assert "base-rule" in msg


def test_supersedes_declaration_appears(tmp_path: Path) -> None:
    scope = tmp_path / "scope"
    a = _write_member(
        scope,
        "a.md",
        "body a",
        supersedes=[{"name": "old-memory", "as_of": "2026-01-01", "reason": "stale"}],
    )
    b = _write_member(scope, "b.md", "body b")
    msg = _build_user_message(
        _detected([a, b], ["passage a", "passage b"]),
        [a, b],
    )
    assert "supersedes:" in msg
    assert "old-memory" in msg


# ---------------------------------------------------------------------------
# Token-cap config
# ---------------------------------------------------------------------------


def test_token_cap_env_override(monkeypatch) -> None:
    monkeypatch.setenv("ATHENAEUM_RESOLVE_FULL_BODY_TOKEN_CAP", "42")
    assert resolve_full_body_token_cap({}) == 42


def test_token_cap_config_setting() -> None:
    cfg = {"resolve": {"full_body_token_cap": 250}}
    assert resolve_full_body_token_cap(cfg) == 250


def test_token_cap_default(monkeypatch) -> None:
    monkeypatch.delenv("ATHENAEUM_RESOLVE_FULL_BODY_TOKEN_CAP", raising=False)
    assert resolve_full_body_token_cap(None) == DEFAULT_FULL_BODY_TOKEN_CAP


# ---------------------------------------------------------------------------
# Backward compat
# ---------------------------------------------------------------------------


def test_build_user_message_without_config_argument(tmp_path: Path) -> None:
    """Existing two-arg signature still works (config defaults to None)."""
    scope = tmp_path / "scope"
    a = _write_member(scope, "a.md", "body a")
    b = _write_member(scope, "b.md", "body b")
    # No third arg.
    msg = _build_user_message(
        _detected([a, b], ["passage a", "passage b"]),
        [a, b],
    )
    assert "Member a:" in msg
    assert "Member b:" in msg

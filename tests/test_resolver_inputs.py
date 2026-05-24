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

import pytest

from athenaeum.contradictions import ContradictionResult
from athenaeum.models import AutoMemoryFile
from athenaeum.resolutions import (
    _RESOLVE_SYSTEM,
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


# ---------------------------------------------------------------------------
# Quine review #1 — passage always emitted alongside body
# ---------------------------------------------------------------------------


def test_passage_always_present_with_small_body(tmp_path: Path) -> None:
    """Quine #1 — passage line must appear even when body fits the cap."""
    scope = tmp_path / "scope"
    a = _write_member(scope, "a.md", "Full body of A here.")
    b = _write_member(scope, "b.md", "Full body of B here.")
    msg = _build_user_message(
        _detected([a, b], ["pinpoint A", "pinpoint B"]),
        [a, b],
    )
    # Both the passage AND the body are present on each side.
    assert "passage: pinpoint A" in msg
    assert "passage: pinpoint B" in msg
    assert "Full body of A here." in msg
    assert "Full body of B here." in msg
    # No truncation note on the small-body path.
    assert "truncated" not in msg


def test_truncation_note_wording_on_asymmetric_path(tmp_path: Path) -> None:
    """Quine #1 — truncation note reads 'passage above is the conflict region'."""
    scope = tmp_path / "scope"
    a = _write_member(scope, "a.md", "small body")
    big_body = "Y" * 5000
    b = _write_member(scope, "b.md", big_body)
    cfg = {"resolve": {"full_body_token_cap": 20}}
    msg = _build_user_message(
        _detected([a, b], ["passage a", "passage b"]),
        [a, b],
        cfg,
    )
    assert "passage above is the conflict region" in msg
    assert "showing passage only" not in msg
    # Passage still emitted on truncated side.
    assert "passage: passage b" in msg


def test_system_prompt_describes_passage_and_body_contract() -> None:
    """Quine #1 — prompt accurately describes the payload shape."""
    assert "passage" in _RESOLVE_SYSTEM
    assert "body" in _RESOLVE_SYSTEM
    # Pin the specific contract sentence so any drift surfaces.
    assert "always provided" in _RESOLVE_SYSTEM
    assert "full body is also included" in _RESOLVE_SYSTEM


# ---------------------------------------------------------------------------
# Quine review #2 — link search space unions body + passage
# ---------------------------------------------------------------------------


def test_link_search_space_unions_body_and_passage(tmp_path: Path) -> None:
    """Quine #2 — link in passage must resolve even when body has none."""
    scope = tmp_path / "scope"
    _write_member(
        scope,
        "target.md",
        "Target body.",
        name="passage-linked",
        description="Passage-linked description string.",
    )
    # Body has no wikilink; the detector's passage has one.
    a = _write_member(scope, "a.md", "Body of A without any links inside.")
    b = _write_member(scope, "b.md", "Body of B.")
    msg = _build_user_message(
        _detected(
            [a, b],
            ["See [[passage-linked]] in this passage.", "passage b"],
        ),
        [a, b],
    )
    assert "link[passage-linked]" in msg
    assert "Passage-linked description string." in msg


# ---------------------------------------------------------------------------
# Quine review #4 — cap=0 and negative values are rejected
# ---------------------------------------------------------------------------


def test_token_cap_zero_rejected() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        resolve_full_body_token_cap({"resolve": {"full_body_token_cap": 0}})


def test_token_cap_negative_rejected() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        resolve_full_body_token_cap({"resolve": {"full_body_token_cap": -5}})


def test_token_cap_zero_rejected_via_env(monkeypatch) -> None:
    monkeypatch.setenv("ATHENAEUM_RESOLVE_FULL_BODY_TOKEN_CAP", "0")
    with pytest.raises(ValueError, match="positive integer"):
        resolve_full_body_token_cap({})


def test_token_cap_negative_rejected_via_env(monkeypatch) -> None:
    monkeypatch.setenv("ATHENAEUM_RESOLVE_FULL_BODY_TOKEN_CAP", "-10")
    with pytest.raises(ValueError, match="positive integer"):
        resolve_full_body_token_cap({})


# ---------------------------------------------------------------------------
# Issue #175 — boundary and tolerance tests
# ---------------------------------------------------------------------------


def test_body_exactly_at_char_cap_is_included(tmp_path: Path) -> None:
    """Body length == cap*4 chars → included with NO truncation note.

    Boundary is ``> char_cap`` (strict), so equal-length bodies stay in.
    """
    scope = tmp_path / "scope"
    # cap = 5 tokens × 4 chars/token = 20 chars exactly.
    body = "X" * 20
    a = _write_member(scope, "a.md", body)
    b = _write_member(scope, "b.md", "small")
    cfg = {"resolve": {"full_body_token_cap": 5}}
    msg = _build_user_message(
        _detected([a, b], ["pa", "pb"]),
        [a, b],
        cfg,
    )
    assert body in msg
    # Truncation note must NOT fire for member a at the exact boundary.
    # (member b is fine — body is shorter than the cap.)
    assert msg.count("truncated") == 0


def test_body_one_char_over_cap_is_truncated(tmp_path: Path) -> None:
    """Body length == cap*4 + 1 chars → truncated, passage only."""
    scope = tmp_path / "scope"
    body = "X" * 21  # 5 tokens × 4 chars = 20, +1 → truncate.
    a = _write_member(scope, "a.md", body)
    b = _write_member(scope, "b.md", "small")
    cfg = {"resolve": {"full_body_token_cap": 5}}
    msg = _build_user_message(
        _detected([a, b], ["pa", "pb"]),
        [a, b],
        cfg,
    )
    assert body not in msg
    assert "truncated" in msg
    assert "5-token budget" in msg


def test_cross_scope_wikilink_silently_dropped(tmp_path: Path) -> None:
    """A wikilink whose target lives in a different scope dir is omitted.

    Cross-scope link resolution is deliberately out of scope — the
    resolver runs per-scope and only the same-scope sibling index is
    consulted.
    """
    scope_a = tmp_path / "scope-a"
    scope_b = tmp_path / "scope-b"
    # Target lives in scope-b…
    _write_member(
        scope_b,
        "target.md",
        "target body",
        name="cross-scope-target",
        description="cross-scope description",
    )
    # …but the linking member lives in scope-a.
    a = _write_member(
        scope_a,
        "a.md",
        "Refers to [[cross-scope-target]] for context.",
    )
    b = _write_member(scope_a, "b.md", "another member")
    msg = _build_user_message(
        _detected([a, b], ["pa", "pb"]),
        [a, b],
    )
    # Link should be silently dropped — no link[...] line, no description echo.
    assert "link[cross-scope-target]" not in msg
    assert "cross-scope description" not in msg


def test_body_read_failure_falls_back_to_passage_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the body read raises OSError the resolver still emits a prompt.

    The member section should fall through to the passage-only path:
    no body, no crash, passage still present.
    """
    scope = tmp_path / "scope"
    a = _write_member(scope, "a.md", "secret body content")
    b = _write_member(scope, "b.md", "other side")

    # Patch Path.read_text on the SPECIFIC files so reading them raises.
    # Other reads (e.g. system prompt fixture loads) must still work.
    target_paths = {a.path.resolve(), b.path.resolve()}
    real_read_text = Path.read_text

    def flaky_read_text(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        try:
            resolved = self.resolve()
        except OSError:
            resolved = self
        if resolved in target_paths:
            raise OSError("simulated read failure")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read_text)

    msg = _build_user_message(
        _detected([a, b], ["passage a", "passage b"]),
        [a, b],
    )
    # Passages are still rendered…
    assert "passage a" in msg
    assert "passage b" in msg
    # …and the body content is NOT (read raised).
    assert "secret body content" not in msg
    assert "other side" not in msg

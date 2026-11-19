# SPDX-License-Identifier: Apache-2.0
"""Owner page: route first-person auto-memory facts to the owner page (athenaeum#2690).

Drives ``librarian.run`` end to end with a stub backend (no network, no key)
over a fresh knowledge root initialised through the real CLI. All people and
facts are invented.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from athenaeum.cli import main
from athenaeum.config import load_config, resolve_owner, resolve_owner_bridge_memory_types
from athenaeum.models import parse_frontmatter
from athenaeum.owner_memory_bridge import (
    BRIDGE_SOURCE,
    ROUTED_KEY,
    bridge_owner_memories,
    restrict_to_owner,
)

OWNER_NAME = "Riley Quill"
FACT_BODY = "The user said: I prefer oat milk in coffee.\n"
FACT_LINE = "- Prefers oat milk in coffee."
THIRD_PARTY_BODY = "The user's colleague Dana Bright prefers tea.\n"
NON_OWNER = "Dana Bright"


class _Stub:
    """Offline stand-in for the Anthropic client; replies by prompt shape."""

    def __init__(self, owner_uid: str | None) -> None:
        self.owner_uid = owner_uid
        self.prompts: list[str] = []
        self.merge_reply: dict[str, Any] = {
            "ops": [{"op": "append_section", "text": "## Preferences\n\n" + FACT_LINE}],
            "adds_new_claim": True,
        }
        self.extra_entities: list[dict[str, Any]] = []
        self.fail_outside_stamps = False
        self.messages = self

    def create(self, **kwargs: Any) -> Any:
        prompt = json.dumps(kwargs.get("messages", ""))
        self.prompts.append(prompt)
        if "<existing_page>" in prompt:
            text = json.dumps(self.merge_reply)
        elif self.owner_uid and f"{self.owner_uid}: {OWNER_NAME} (owner)" in prompt:
            items: list[dict[str, Any]] = [
                {
                    "candidate_uid": self.owner_uid,
                    "observations": "Preference: oat milk in coffee.",
                },
                *self.extra_entities,
            ]
            text = json.dumps(items)
        elif "Fields to determine" in prompt:
            text = "{}"
        else:
            text = "[]"
        response = MagicMock()
        response.content = [MagicMock(text=text)]
        response.stop_reason = "end_turn"
        return response


def _git_commit(path: Path) -> None:
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "seed"],
        cwd=path,
        check=True,
        capture_output=True,
    )


def _write_memory(knowledge: Path, name: str, body: str, mtype: str = "user") -> Path:
    scope = knowledge / "raw" / "auto-memory" / "_unscoped"
    scope.mkdir(parents=True, exist_ok=True)
    path = scope / f"{mtype}_{name}.md"
    path.write_text(
        f"---\nname: {name}\ndescription: a note\nmetadata:\n  type: {mtype}\n"
        f"claim_kind: fact\nbucket: durable\n---\n{body}",
        encoding="utf-8",
    )
    return path


class _Env:
    def __init__(self, knowledge: Path, cache: Path, uid: str | None, stub: _Stub) -> None:
        self.knowledge = knowledge
        self.cache = cache
        self.uid = uid
        self.stub = stub

    def run(self, **kwargs: Any) -> int:
        from athenaeum.librarian import run

        _git_commit(self.knowledge)
        return run(
            raw_root=self.knowledge / "raw",
            wiki_root=self.knowledge / "wiki",
            knowledge_root=self.knowledge,
            **kwargs,
        )

    def bridge_notes(self) -> list[Path]:
        return sorted((self.knowledge / "raw" / BRIDGE_SOURCE).glob("*.md"))

    def pages(self) -> list[Path]:
        return [
            p
            for p in (self.knowledge / "wiki").glob("*.md")
            if not p.name.startswith(("_", "auto-"))  # auto-*.md is the C3 compile of the note
        ]

    def owner_page(self) -> Path | None:
        found = sorted((self.knowledge / "wiki").glob(f"{self.uid}-*.md"))
        return found[0] if found else None


@pytest.fixture
def make_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import anthropic as anthropic_mod

    def _make(*, with_owner: bool = True, label: str = "") -> _Env:
        knowledge = tmp_path / f"knowledge{label}"
        cache = tmp_path / f"cache{label}"
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(cache))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        assert main(["init", "--path", str(knowledge)]) == 0
        uid: str | None = None
        if with_owner:
            assert main(["owner", "set", "--name", OWNER_NAME, "--path", str(knowledge)]) == 0
            owner = resolve_owner(load_config(knowledge))
            assert owner is not None
            uid = owner["uid"]
        # Keep the auto-memory sources on disk so the routed marker can be read
        # back (the retire pass would otherwise move them after compiling).
        cfg = knowledge / "athenaeum.yaml"
        cfg.write_text(
            cfg.read_text(encoding="utf-8").rstrip("\n") + "\nlibrarian:\n  retire: false\n",
            encoding="utf-8",
        )
        stub = _Stub(uid)
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kwargs: stub)
        return _Env(knowledge, cache, uid, stub)

    return _make


def _set_types(env: _Env, types: list[str]) -> None:
    cfg = env.knowledge / "athenaeum.yaml"
    text = cfg.read_text(encoding="utf-8")
    text = re.sub(
        r"^owner:\n",
        "owner:\n  bridge_memory_types: " + json.dumps(types) + "\n",
        text,
        count=1,
        flags=re.M,
    )
    cfg.write_text(text, encoding="utf-8")


def _total_calls(env: _Env) -> int:
    return len(env.stub.prompts)


# --- AC1 ---------------------------------------------------------------------


def test_first_person_note_bridges_then_lands_in_preferences(make_env) -> None:
    env = make_env()
    memory = _write_memory(env.knowledge, "coffee", FACT_BODY)

    assert env.run() == 0
    notes = env.bridge_notes()
    assert len(notes) == 1  # exactly one bridge note on the first run
    assert env.owner_page() is None or FACT_LINE not in env.owner_page().read_text("utf-8")
    meta, _ = parse_frontmatter(memory.read_text(encoding="utf-8"))
    assert meta[ROUTED_KEY]

    assert env.run() == 0  # next run compiles it through the entity tier
    page = env.owner_page()
    assert page is not None
    _, body = parse_frontmatter(page.read_text(encoding="utf-8"))
    assert "## Preferences" in body
    assert FACT_LINE in body
    assert env.bridge_notes() == []  # consumed by the entity loop


# --- AC2 / AC9 ---------------------------------------------------------------


def test_no_owner_is_inert(make_env) -> None:
    env = make_env(with_owner=False)
    memory = _write_memory(env.knowledge, "coffee", FACT_BODY)
    before = memory.read_text(encoding="utf-8")
    assert env.run() == 0
    assert env.bridge_notes() == []
    assert memory.read_text(encoding="utf-8") == before
    assert not (env.knowledge / "raw" / BRIDGE_SOURCE).exists()
    # No model call is attributable to the bridge: the memory is pre-stamped,
    # so with no owner the run makes no call that mentions it at all.
    assert not [p for p in env.stub.prompts if "oat milk" in p]


def test_no_owner_api_calls_match_a_run_without_the_feature(
    make_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import athenaeum.librarian as librarian_mod

    def _unstamped(env: _Env) -> None:
        # No claim_kind/bucket: the stamp passes make real calls, so the count
        # being compared is non-zero.
        path = _write_memory(env.knowledge, "coffee", FACT_BODY)
        path.write_text(
            "---\nname: coffee\nmetadata:\n  type: user\n---\n" + FACT_BODY, encoding="utf-8"
        )

    with_feature = make_env(with_owner=False, label="a")
    _unstamped(with_feature)
    assert with_feature.run() == 0
    assert with_feature.bridge_notes() == []

    baseline = make_env(with_owner=False, label="b")
    _unstamped(baseline)
    monkeypatch.setattr(librarian_mod, "_bridge_owner_memories", lambda ctx, files: 0)
    assert baseline.run() == 0

    assert len(with_feature.stub.prompts) == len(baseline.stub.prompts)
    assert len(with_feature.stub.prompts) > 0


def test_dry_run_writes_no_bridge_note_and_stamps_nothing(make_env) -> None:
    env = make_env()
    memory = _write_memory(env.knowledge, "coffee", FACT_BODY)
    before = memory.read_text(encoding="utf-8")
    assert env.run(dry_run=True) == 0
    assert env.bridge_notes() == []
    assert memory.read_text(encoding="utf-8") == before


# --- AC3 ---------------------------------------------------------------------


def test_once_per_content_and_rebridge_on_edit(make_env) -> None:
    env = make_env()
    memory = _write_memory(env.knowledge, "coffee", FACT_BODY)
    assert env.run() == 0
    assert len(env.bridge_notes()) == 1
    assert env.run() == 0  # entity loop consumes the note
    assert env.bridge_notes() == []

    calls = _total_calls(env)
    assert env.run() == 0  # unchanged + already routed
    assert env.bridge_notes() == []
    assert _total_calls(env) == calls

    meta, body = parse_frontmatter(memory.read_text(encoding="utf-8"))
    memory.write_text(
        memory.read_text(encoding="utf-8") + "I also like it hot.\n", encoding="utf-8"
    )
    assert env.run() == 0
    assert len(env.bridge_notes()) == 1  # edited body re-bridges exactly once
    assert env.run() == 0
    assert env.bridge_notes() == []
    assert env.run() == 0
    assert env.bridge_notes() == []


# --- AC4 ---------------------------------------------------------------------


def test_other_types_are_never_bridged(make_env) -> None:
    env = make_env()
    for mtype in ("feedback", "project", "reference"):
        _write_memory(env.knowledge, f"n_{mtype}", FACT_BODY, mtype)
    assert env.run() == 0
    assert env.bridge_notes() == []


def test_empty_list_disables_the_bridge(make_env) -> None:
    env = make_env()
    _set_types(env, [])
    memory = _write_memory(env.knowledge, "coffee", FACT_BODY)
    before = memory.read_text(encoding="utf-8")
    assert env.run() == 0
    assert env.bridge_notes() == []
    assert memory.read_text(encoding="utf-8") == before


def test_configured_list_selects_other_types(make_env) -> None:
    env = make_env()
    _set_types(env, ["feedback"])
    _write_memory(env.knowledge, "n_fb", FACT_BODY, "feedback")
    _write_memory(env.knowledge, "n_user", FACT_BODY, "user")
    assert env.run() == 0
    assert len(env.bridge_notes()) == 1


def test_resolver_defaults_and_cleaning() -> None:
    assert resolve_owner_bridge_memory_types(None) == ["user"]
    assert resolve_owner_bridge_memory_types({"owner": {"uid": "x"}}) == ["user"]
    assert resolve_owner_bridge_memory_types({"owner": {"bridge_memory_types": []}}) == []
    assert resolve_owner_bridge_memory_types(
        {"owner": {"bridge_memory_types": ["User", "bogus", "user", "feedback"]}}
    ) == ["user", "feedback"]
    assert resolve_owner_bridge_memory_types({"owner": {"bridge_memory_types": "user"}}) == ["user"]


# --- AC5 ---------------------------------------------------------------------


def test_owner_only_scope_drops_non_owner_entities(make_env) -> None:
    env = make_env()
    env.stub.extra_entities = [
        {
            "name": NON_OWNER,
            "entity_type": "person",
            "tags": [],
            "access": "internal",
            "observations": "Dana Bright prefers tea.",
        }
    ]
    _write_memory(env.knowledge, "mixed", FACT_BODY + THIRD_PARTY_BODY)
    assert env.run() == 0
    assert env.run() == 0
    page = env.owner_page()
    assert page is not None and FACT_LINE in page.read_text(encoding="utf-8")
    assert [p for p in env.pages() if NON_OWNER.split()[0].lower() in p.name.lower()] == []
    assert len(env.pages()) == 1  # only the owner page exists
    # No tier-3 call was ever made for the non-owner entity.
    assert not [p for p in env.stub.prompts if "Create a new wiki" in p and NON_OWNER in p]


def test_restrict_to_owner_unit() -> None:
    from athenaeum.models import ClassifiedEntity

    owner = ClassifiedEntity("O", "person", [], "", False, existing_uid="u1", from_person_hint=True)
    other = ClassifiedEntity("D", "person", [], "", False, existing_uid="u2", from_person_hint=True)
    new = ClassifiedEntity("N", "person", [], "", True)
    assert restrict_to_owner([owner, other, new], "u1", "r") == [owner]
    assert restrict_to_owner([owner], None, "r") == []


# --- AC6 ---------------------------------------------------------------------


def test_third_party_subject_mismatch_leaves_owner_page_unchanged(make_env) -> None:
    env = make_env()
    env.stub.merge_reply = {"ops": [], "adds_new_claim": False, "subject_mismatch": True}
    _write_memory(env.knowledge, "colleague", THIRD_PARTY_BODY)
    assert env.run() == 0
    assert env.run() == 0
    page = env.owner_page()
    text = page.read_text(encoding="utf-8") if page else ""
    assert "tea" not in text
    assert "## Preferences" not in text


# --- AC7 ---------------------------------------------------------------------


def test_bridge_makes_no_model_call(make_env, monkeypatch: pytest.MonkeyPatch) -> None:
    import inspect

    import athenaeum.librarian as librarian_mod

    assert "client" not in inspect.signature(bridge_owner_memories).parameters

    def _unstamped(env: _Env) -> None:
        path = _write_memory(env.knowledge, "coffee", FACT_BODY)
        path.write_text(
            "---\nname: coffee\nmetadata:\n  type: user\n---\n" + FACT_BODY, encoding="utf-8"
        )

    # Unstamped memory: the claim_kind and bucket stamps are the only calls the
    # auto-memory phase may make. The bridge adds none on top of them.
    bridged = make_env(label="a")
    _unstamped(bridged)
    assert bridged.run() == 0
    assert len(bridged.bridge_notes()) == 1

    baseline = make_env(label="b")
    _unstamped(baseline)
    monkeypatch.setattr(librarian_mod, "_bridge_owner_memories", lambda ctx, files: 0)
    assert baseline.run() == 0
    assert baseline.bridge_notes() == []

    assert len(bridged.stub.prompts) == len(baseline.stub.prompts) > 0


# --- AC8 ---------------------------------------------------------------------


def test_budget_spent_leaves_bridge_notes_for_a_later_run(make_env) -> None:
    env = make_env()
    _write_memory(env.knowledge, "coffee", FACT_BODY)
    assert env.run() == 0
    assert len(env.bridge_notes()) == 1
    calls = _total_calls(env)
    assert env.run(max_api_calls=0) in (0, 3, 75)
    assert len(env.bridge_notes()) == 1  # deferred, not dropped
    assert not [p for p in env.stub.prompts[calls:] if "oat milk" in p]
    assert env.run() == 0  # a later run with budget processes it
    assert env.bridge_notes() == []
    page = env.owner_page()
    assert page is not None and FACT_LINE in page.read_text(encoding="utf-8")


# --- AC10 --------------------------------------------------------------------


def test_run_summary_reports_notes_bridged(make_env, caplog: pytest.LogCaptureFixture) -> None:
    env = make_env()
    _write_memory(env.knowledge, "coffee", FACT_BODY)
    with caplog.at_level(logging.INFO):
        assert env.run() == 0
    summaries = [
        r.getMessage() for r in caplog.records if "librarian-run-summary" in r.getMessage()
    ]
    assert summaries
    assert any("owner_bridged=1" in s for s in summaries)


# --- Unparseable frontmatter (Seer review on PR athenaeum#2803) ---------------


def test_unparseable_frontmatter_is_left_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source whose frontmatter does not parse is neither bridged nor stamped.

    ``parse_frontmatter`` returns ``({}, whole text)`` for a broken block, so
    stamping would re-render the broken block into the body as plain text.
    """
    from types import SimpleNamespace

    import athenaeum.owner_memory_bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "owner_uid_from_config", lambda config: "own1")
    monkeypatch.setattr(bridge_mod, "_owner_candidate", lambda uid, index, reg: object())
    broken = "---\nname: [unclosed\nmetadata: {type: user\n---\n" + FACT_BODY
    good_text = "---\nname: coffee\n---\n" + FACT_BODY
    src_dir = tmp_path / "memories"
    src_dir.mkdir()
    bad = src_dir / "bad.md"
    bad.write_text(broken, encoding="utf-8")
    good = src_dir / "good.md"
    good.write_text(good_text, encoding="utf-8")

    def _am(path: Path) -> Any:
        return SimpleNamespace(
            path=path, memory_type="user", origin_scope="scope", origin_session_id=""
        )

    raw_root = tmp_path / "raw"
    count = bridge_owner_memories(
        [_am(bad), _am(good)],
        config={"owner": {"name": OWNER_NAME}},
        raw_root=raw_root,
        index=MagicMock(),
        person_registry=None,
    )
    assert count == 1  # only the parseable note
    assert bad.read_text(encoding="utf-8") == broken  # byte-identical, unstamped
    meta, body = parse_frontmatter(good.read_text(encoding="utf-8"))
    assert meta.get(ROUTED_KEY) and body == FACT_BODY
    assert len(list((raw_root / BRIDGE_SOURCE).glob("*.md"))) == 1

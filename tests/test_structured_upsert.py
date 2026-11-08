# SPDX-License-Identifier: Apache-2.0
"""Tier-0 structured upsert for configured entity types (issue athenaeum#2360).

The fixtures are a real recipe-shaped page: a link-only stub that later
information (the full recipe, then a note) must update in place. Nothing in the
code under test names "recipe"; that type exists only in this test's config.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.config import resolve_structured_upsert
from athenaeum.librarian import process_one, tier0_structured_upsert
from athenaeum.models import EntityIndex, RawFile, parse_frontmatter

UID = "recipe-lemon-pistachio-loaf"
TYPES = ["recipe", "company", "concept"]

CONFIG: dict = {
    "librarian": {
        "structured_upsert": {
            "recipe": {
                "sections": ["Ingredients", "Steps", "Notes", "Source"],
                "replace_sections": ["Ingredients", "Steps"],
                "append_sections": ["Notes", "Source"],
                "union_keys": ["tags", "shared_by", "source_messages"],
                "keep_keys": ["shared_at"],
                "set_keys": [
                    "recipe_url",
                    "recipe_author",
                    "recipe_site",
                    "cuisine",
                    "course",
                    "servings",
                    "prep_time",
                    "cook_time",
                    "total_time",
                    "source",
                ],
                "ordered_keys": {"fetch_status": ["video", "attachment-only", "link-only", "full"]},
            }
        }
    }
}

LINK_ONLY_PAGE = """\
---
uid: recipe-lemon-pistachio-loaf
type: recipe
name: Lemon-Pistachio Loaf
access: personal
created: '2026-10-01'
updated: '2026-10-01'
tags:
  - baking
source: email:msg-001
shared_at: '2026-10-01'
shared_by:
  - person-dana-reyes
source_messages:
  - msg-001
fetch_status: link-only
recipe_url: https://bakes.example/lemon-pistachio-loaf
---

# Lemon-Pistachio Loaf

## Notes

- Dana says it freezes well.

## Source

- https://bakes.example/lemon-pistachio-loaf
"""

FULL_RAW = """\
---
uid: recipe-lemon-pistachio-loaf
type: recipe
name: Lemon-Pistachio Loaf
access: public
created: '2026-11-08'
tags:
  - baking
  - citrus
source: email:msg-001
shared_at: '2026-11-08'
shared_by:
  - person-dana-reyes
  - person-lee-park
source_messages:
  - msg-002
fetch_status: full
recipe_author: Pat Baker
recipe_site: bakes.example
servings: '8'
---

# Lemon Pistachio Loaf (imported)

## Ingredients

### For the loaf

- 200 g flour
- 2 lemons, zested

### For the topping

- 80 g pistachios

## Steps

1. Heat the oven to 175 C.
2. Fold everything together.
3. Bake for 45 minutes.

## Notes

- Dana says it freezes well.
- Lee swapped in almonds.

## Source

- https://bakes.example/lemon-pistachio-loaf
- Forwarded by Lee
"""

NOTE_ONLY_RAW = """\
---
uid: recipe-lemon-pistachio-loaf
type: recipe
name: Lemon-Pistachio Loaf
fetch_status: link-only
---

# Lemon-Pistachio Loaf

## Notes

- Emily found it a touch sweet.
"""


@pytest.fixture
def wiki(tmp_path: Path) -> Path:
    w = tmp_path / "wiki"
    w.mkdir()
    (w / f"{UID}.md").write_text(LINK_ONLY_PAGE, encoding="utf-8")
    return w


def _raw(tmp_path: Path, content: str, name: str = "r1.md") -> RawFile:
    d = tmp_path / "raw" / "recipes"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(content, encoding="utf-8")
    return RawFile(path=p, source="recipes", timestamp="", uuid8="")


def _page(wiki: Path) -> tuple[dict, str]:
    return parse_frontmatter((wiki / f"{UID}.md").read_text(encoding="utf-8"))


def _upsert(raw: RawFile, wiki: Path, **kw):
    return tier0_structured_upsert(raw, EntityIndex(wiki), wiki, TYPES, config=CONFIG, **kw)


def _headings(body: str) -> list[str]:
    return [ln for ln in body.splitlines() if ln.startswith("#")]


def test_link_only_to_full_inserts_sections_in_canonical_order(tmp_path: Path, wiki: Path) -> None:
    out = _upsert(_raw(tmp_path, FULL_RAW), wiki)
    assert out is not None and out.status == "applied" and out.uid == UID
    meta, body = _page(wiki)
    # The page's own H1 survives; the raw's different H1 is not adopted.
    assert _headings(body) == [
        "# Lemon-Pistachio Loaf",
        "## Ingredients",
        "### For the loaf",
        "### For the topping",
        "## Steps",
        "## Notes",
        "## Source",
    ]
    assert "imported" not in body
    assert "200 g flour" in body and "Bake for 45 minutes." in body
    assert meta["fetch_status"] == "full"
    assert meta["tags"] == ["baking", "citrus"]
    assert meta["shared_by"] == ["person-dana-reyes", "person-lee-park"]
    assert meta["source_messages"] == ["msg-001", "msg-002"]
    assert meta["recipe_author"] == "Pat Baker"
    assert meta["servings"] == "8"
    assert meta["shared_at"] == "2026-10-01"  # not a configured key: untouched
    # Notes and Source: existing line kept once, new lines appended.
    assert body.count("Dana says it freezes well.") == 1
    assert "Lee swapped in almonds." in body and "Forwarded by Lee" in body
    # Identity envelope untouched even though the raw disagreed.
    assert (meta["uid"], meta["type"], meta["created"], meta["access"]) == (
        UID,
        "recipe",
        "2026-10-01",
        "personal",
    )
    assert meta["updated"] != "2026-10-01"


def test_reapply_is_a_byte_identical_noop(tmp_path: Path, wiki: Path) -> None:
    _upsert(_raw(tmp_path, FULL_RAW), wiki)
    path = wiki / f"{UID}.md"
    after_first = path.read_text(encoding="utf-8")
    mtime = path.stat().st_mtime_ns
    out = _upsert(_raw(tmp_path, FULL_RAW, "r2.md"), wiki)
    assert out is not None and out.status == "noop"
    assert path.read_text(encoding="utf-8") == after_first
    assert path.stat().st_mtime_ns == mtime  # no write at all


def test_note_only_raw_appends_notes_and_leaves_structure(tmp_path: Path, wiki: Path) -> None:
    _upsert(_raw(tmp_path, FULL_RAW), wiki)
    _, before = _page(wiki)
    out = _upsert(_raw(tmp_path, NOTE_ONLY_RAW, "r2.md"), wiki)
    assert out is not None and out.status == "applied"
    meta, body = _page(wiki)
    assert "Emily found it a touch sweet." in body
    # Ingredients and Steps byte-identical: an empty raw section replaces nothing.
    for title in ("## Ingredients", "## Steps"):
        assert body.split(title)[1].split("\n## ")[0] == before.split(title)[1].split("\n## ")[0]
    assert meta["fetch_status"] == "full"  # a link-only raw never downgrades


def test_fetch_status_never_downgrades_and_missing_rank_is_ignored(
    tmp_path: Path, wiki: Path
) -> None:
    _upsert(_raw(tmp_path, FULL_RAW), wiki)
    odd = NOTE_ONLY_RAW.replace("link-only", "mystery")
    out = _upsert(_raw(tmp_path, odd, "r2.md"), wiki)
    assert out is not None
    assert _page(wiki)[0]["fetch_status"] == "full"


def test_empty_set_key_never_blanks_existing_value(tmp_path: Path, wiki: Path) -> None:
    raw = NOTE_ONLY_RAW.replace("fetch_status: link-only", "recipe_url: ''")
    _upsert(_raw(tmp_path, raw), wiki)
    assert _page(wiki)[0]["recipe_url"] == "https://bakes.example/lemon-pistachio-loaf"


def test_type_mismatch_is_refused_loudly(
    tmp_path: Path, wiki: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (wiki / f"{UID}.md").write_text(
        LINK_ONLY_PAGE.replace("type: recipe", "type: concept"), encoding="utf-8"
    )
    before = (wiki / f"{UID}.md").read_text(encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        out = _upsert(_raw(tmp_path, FULL_RAW), wiki)
    assert out is not None and out.status == "refused"
    assert out.skipped_label == "structured-upsert-type-mismatch"
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert (wiki / f"{UID}.md").read_text(encoding="utf-8") == before


def test_unconfigured_type_and_no_config_are_inert(tmp_path: Path, wiki: Path) -> None:
    raw = _raw(tmp_path, FULL_RAW.replace("type: recipe", "type: company"))
    assert _upsert(raw, wiki) is None
    assert (
        tier0_structured_upsert(
            _raw(tmp_path, FULL_RAW, "r3.md"), EntityIndex(wiki), wiki, TYPES, config=None
        )
        is None
    )
    assert (wiki / f"{UID}.md").read_text(encoding="utf-8") == LINK_ONLY_PAGE


def test_unconfigured_raw_section_is_refused_not_dropped(tmp_path: Path, wiki: Path) -> None:
    raw = FULL_RAW + "\n## Nutrition\n\n- 300 kcal\n"
    out = _upsert(_raw(tmp_path, raw), wiki)
    assert out is not None and out.status == "refused"
    assert out.reason == "unappliable"
    assert (wiki / f"{UID}.md").read_text(encoding="utf-8") == LINK_ONLY_PAGE


def test_hash_comment_in_fenced_block_is_not_a_heading(tmp_path: Path, wiki: Path) -> None:
    raw = FULL_RAW.replace("3. Bake for 45 minutes.", "```\n## not a heading\n```")
    out = _upsert(_raw(tmp_path, raw), wiki)
    assert out is not None and out.status == "applied"
    assert "## not a heading" in _page(wiki)[1]


def test_dry_run_writes_nothing(tmp_path: Path, wiki: Path) -> None:
    out = _upsert(_raw(tmp_path, FULL_RAW), wiki, dry_run=True)
    assert out is not None and out.status == "applied"
    assert (wiki / f"{UID}.md").read_text(encoding="utf-8") == LINK_ONLY_PAGE


def test_config_strips_identity_keys_and_stray_sections() -> None:
    specs = resolve_structured_upsert(
        {
            "librarian": {
                "structured_upsert": {
                    "recipe": {
                        "sections": ["Steps"],
                        "replace_sections": ["Steps", "Elsewhere"],
                        "set_keys": ["uid", "access", "servings"],
                        "union_keys": ["type", "tags"],
                    },
                    "bad": "not-a-dict",
                }
            }
        }
    )
    assert set(specs) == {"recipe"}
    assert specs["recipe"].set_keys == ("servings",)
    assert specs["recipe"].union_keys == ("tags",)
    assert specs["recipe"].replace_sections == frozenset({"steps"})
    assert resolve_structured_upsert(None) == {}
    assert resolve_structured_upsert({"librarian": []}) == {}


# --- process_one wiring: no LLM, refusal never falls through ------------------


def _no_llm() -> MagicMock:
    client = MagicMock(name="llm")
    client.side_effect = AssertionError("LLM tier reached")
    return client


def _run(raw: RawFile, wiki: Path, client: MagicMock):
    return process_one(
        raw,
        EntityIndex(wiki),
        wiki,
        client,
        TYPES,
        ["baking"],
        ["internal", "public"],
        config=CONFIG,
    )


def test_process_one_applies_without_llm(tmp_path: Path, wiki: Path) -> None:
    client = _no_llm()
    result = _run(_raw(tmp_path, FULL_RAW), wiki, client)
    assert result.updated == [UID]
    assert not result.skipped and not result.retain_raw and not result.created
    assert client.mock_calls == []


def test_process_one_refusal_skips_retains_and_never_reaches_llm(
    tmp_path: Path, wiki: Path
) -> None:
    (wiki / f"{UID}.md").write_text(
        LINK_ONLY_PAGE.replace("type: recipe", "type: concept"), encoding="utf-8"
    )
    client = _no_llm()
    raw = _raw(tmp_path, FULL_RAW)
    result = _run(raw, wiki, client)
    assert result.skipped == ["structured-upsert-type-mismatch"]
    assert result.retain_raw is True
    assert not result.updated and not result.created
    assert client.mock_calls == []
    assert raw.path.exists()  # process_one itself never deletes the raw


class _ReachedNextTier(Exception):
    pass


def test_process_one_unconfigured_falls_through_to_the_next_tier_untouched(
    tmp_path: Path, wiki: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No structured_upsert config: the raw is not claimed and proceeds, exactly as it
    did before this step existed, to the uid-bind / Tier-3 path (a sentinel stands in
    for the LLM write tier). The page is untouched when it gets there."""

    def reached(*_a: object, **_k: object) -> None:
        raise _ReachedNextTier

    monkeypatch.setattr("athenaeum.librarian.tier3_derive_actions", reached)
    raw = _raw(tmp_path, FULL_RAW)
    with pytest.raises(_ReachedNextTier):
        process_one(
            raw,
            EntityIndex(wiki),
            wiki,
            MagicMock(),
            TYPES,
            ["baking"],
            ["internal", "public"],
            config={"librarian": {}},
        )
    assert (wiki / f"{UID}.md").read_text(encoding="utf-8") == LINK_ONLY_PAGE
    assert raw.path.read_text(encoding="utf-8") == FULL_RAW


@pytest.mark.parametrize(
    ("raw_text", "label"),
    [
        (
            FULL_RAW.replace(f"uid: {UID}\n", ""),
            "structured-upsert-ineligible",
        ),
    ],
    ids=["ineligible"],
)
def test_process_one_ineligible_refuses(
    tmp_path: Path, wiki: Path, raw_text: str, label: str
) -> None:
    client = _no_llm()
    raw = _raw(tmp_path, raw_text)
    result = _run(raw, wiki, client)
    assert result.skipped == [label]
    assert result.retain_raw is True
    assert not result.updated and not result.created
    assert client.mock_calls == []
    assert (wiki / f"{UID}.md").read_text(encoding="utf-8") == LINK_ONLY_PAGE


def test_process_one_unresolved_uid_refuses(tmp_path: Path, wiki: Path) -> None:
    """The index knows the uid (so passthrough declines) but the page file is gone.

    A uid the index has never seen is a NEW page and is created by passthrough, so
    this stale-index shape is how a configured raw reaches the unresolved refusal."""
    index = EntityIndex(wiki)
    (wiki / f"{UID}.md").unlink()
    client = _no_llm()
    raw = _raw(tmp_path, FULL_RAW)
    result = process_one(
        raw, index, wiki, client, TYPES, ["baking"], ["internal", "public"], config=CONFIG
    )
    assert result.skipped == ["structured-upsert-unresolved-uid"]
    assert result.retain_raw is True
    assert not result.updated and not result.created
    assert client.mock_calls == []


# --- refusals added after review: preamble, open fence, rank gate --------------

INTRO_RAW = FULL_RAW.replace(
    "# Lemon Pistachio Loaf (imported)\n\n## Ingredients",
    "# Lemon Pistachio Loaf (imported)\n\nA bright, nutty loaf from Pat's blog.\n\n## Ingredients",
)
NO_H2_RAW = NOTE_ONLY_RAW.split("## Notes")[0] + "Emily: halve the sugar next time.\n"
OPEN_FENCE_RAW = FULL_RAW.replace("- 80 g pistachios", "- 80 g pistachios\n```")


@pytest.mark.parametrize(
    "raw_text", [INTRO_RAW, NO_H2_RAW, OPEN_FENCE_RAW], ids=["intro", "no-h2", "open-fence"]
)
def test_unplaceable_raw_body_is_refused_unappliable(
    tmp_path: Path, wiki: Path, raw_text: str
) -> None:
    client = _no_llm()
    raw = _raw(tmp_path, raw_text)
    result = _run(raw, wiki, client)
    assert result.skipped == ["structured-upsert-unappliable"]
    assert result.retain_raw is True and not result.updated
    assert client.mock_calls == []
    assert (wiki / f"{UID}.md").read_text(encoding="utf-8") == LINK_ONLY_PAGE


@pytest.mark.parametrize("raw_text", [INTRO_RAW, NO_H2_RAW], ids=["intro", "no-h2"])
def test_run_keeps_raw_with_unplaceable_preamble_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw_text: str
) -> None:
    root = _seed_root(tmp_path, LINK_ONLY_PAGE)
    raw_path = root / "raw" / "recipes" / "20261108T120000Z-aabbccdd.md"
    raw_path.write_text(raw_text, encoding="utf-8")
    _run_loop(root, monkeypatch)
    assert raw_path.read_text(encoding="utf-8") == raw_text
    assert (root / "wiki" / f"{UID}.md").read_text(encoding="utf-8") == LINK_ONLY_PAGE


def test_lower_ranked_raw_does_not_replace_sections_but_still_appends(
    tmp_path: Path, wiki: Path
) -> None:
    _upsert(_raw(tmp_path, FULL_RAW), wiki)
    _, full_body = _page(wiki)
    attach = (
        FULL_RAW.replace("fetch_status: full", "fetch_status: attachment-only")
        .replace("- 200 g flour", "- 999 g mystery")
        .replace("- Lee swapped in almonds.", "- Photo attached from Lee.")
        .replace("  - citrus", "  - citrus\n  - holiday")
    )
    out = _upsert(_raw(tmp_path, attach, "r2.md"), wiki)
    assert out is not None and out.status == "applied"
    meta, body = _page(wiki)
    assert "200 g flour" in body and "999 g mystery" not in body  # Ingredients kept
    assert "Photo attached from Lee." in body  # append sections still apply
    assert "holiday" in meta["tags"]  # union keys still apply
    assert meta["fetch_status"] == "full"
    assert body.split("## Ingredients")[1].split("\n## ")[0] == (
        full_body.split("## Ingredients")[1].split("\n## ")[0]
    )


# --- batch transport ----------------------------------------------------------


def _batch(root: Path, raw: RawFile, wiki: Path):
    from athenaeum.batch import process_batch_run
    from athenaeum.models import TokenUsage

    client = MagicMock()
    client.messages.create.side_effect = AssertionError("LLM reached")
    client.messages.batches.create.side_effect = AssertionError("batch client reached")
    result = process_batch_run(
        [raw],
        EntityIndex(wiki),
        wiki,
        client,
        TYPES,
        ["baking"],
        ["internal", "public"],
        usage=TokenUsage(),
        config=CONFIG,
        max_api_calls=100,
        cache_dir=root / "cache",
        sleep=lambda _s: None,
    )
    return result, client


def test_batch_applies_and_deletes_raw(tmp_path: Path, wiki: Path) -> None:
    raw = _raw(tmp_path, FULL_RAW)
    result, client = _batch(tmp_path, raw, wiki)
    assert result.updated == 1 and result.skipped == 0 and result.created == 0
    assert not raw.path.exists()
    assert _page(wiki)[0]["fetch_status"] == "full"
    assert client.mock_calls == []


def test_batch_refusal_retains_raw(tmp_path: Path, wiki: Path) -> None:
    (wiki / f"{UID}.md").write_text(
        LINK_ONLY_PAGE.replace("type: recipe", "type: concept"), encoding="utf-8"
    )
    raw = _raw(tmp_path, FULL_RAW)
    result, client = _batch(tmp_path, raw, wiki)
    assert result.skipped == 1 and result.updated == 0
    assert raw.path.read_text(encoding="utf-8") == FULL_RAW
    assert client.mock_calls == []


# --- run() loop: applied raws are consumed, refused raws stay on disk ---------


def _seed_root(tmp_path: Path, page_text: str) -> Path:
    import subprocess

    root = tmp_path / "knowledge"
    wiki = root / "wiki"
    (wiki / "_schema").mkdir(parents=True)
    (wiki / "_schema" / "types.md").write_text(
        "# Types\n\n| Type |\n|------|\n| recipe |\n| concept |\n"
    )
    (wiki / "_schema" / "tags.md").write_text("# Tags\n\n| Tag |\n|-----|\n| baking |\n")
    (wiki / "_schema" / "access-levels.md").write_text(
        "# Access\n\n| Level |\n|-------|\n| internal |\n"
    )
    (wiki / f"{UID}.md").write_text(page_text, encoding="utf-8")
    (root / "athenaeum.yaml").write_text(
        "librarian:\n  structured_upsert:\n    recipe:\n"
        "      sections: [Ingredients, Steps, Notes, Source]\n"
        "      replace_sections: [Ingredients, Steps]\n"
        "      append_sections: [Notes, Source]\n"
        "      union_keys: [tags, shared_by, source_messages]\n"
        "      keep_keys: [shared_at]\n"
        "      set_keys: [source, recipe_author, recipe_site, servings]\n"
        "      ordered_keys:\n        fetch_status: [link-only, full]\n",
        encoding="utf-8",
    )
    (root / "raw" / "recipes").mkdir(parents=True)
    for cmd in (
        ["git", "init", "-q", "-b", "test-branch"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test Runner"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "seed"],
    ):
        subprocess.run(cmd, cwd=root, check=True)
    return root


def _run_loop(root: Path, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    import anthropic as anthropic_mod

    from athenaeum.librarian import run

    client = MagicMock()
    client.messages.create.side_effect = AssertionError("LLM tier reached")
    monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kwargs: client)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    run(raw_root=root / "raw", wiki_root=root / "wiki", knowledge_root=root)
    return client


def test_run_consumes_applied_raw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _seed_root(tmp_path, LINK_ONLY_PAGE)
    raw_path = root / "raw" / "recipes" / "20261108T120000Z-aabbccdd.md"
    raw_path.write_text(FULL_RAW, encoding="utf-8")
    client = _run_loop(root, monkeypatch)
    assert client.messages.create.call_count == 0
    assert not raw_path.exists()
    meta, body = parse_frontmatter((root / "wiki" / f"{UID}.md").read_text(encoding="utf-8"))
    assert meta["fetch_status"] == "full" and "200 g flour" in body


def test_run_leaves_refused_raw_on_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    page = LINK_ONLY_PAGE.replace("type: recipe", "type: concept")
    root = _seed_root(tmp_path, page)
    raw_path = root / "raw" / "recipes" / "20261108T120000Z-aabbccdd.md"
    raw_path.write_text(FULL_RAW, encoding="utf-8")
    client = _run_loop(root, monkeypatch)
    assert client.messages.create.call_count == 0
    assert raw_path.read_text(encoding="utf-8") == FULL_RAW  # still there, untouched
    assert (root / "wiki" / f"{UID}.md").read_text(encoding="utf-8") == page


# --- final review: preamble hole, unranked raw value, CommonMark fences ---------

_H1_RAW = "# Lemon Pistachio Loaf (imported)\n\n## Ingredients"


@pytest.mark.parametrize(
    "loose",
    ["    # preheat to 180C", "# Serves 8, keeps 3 days", "### Overview", "<!-- imported -->"],
    ids=["indented-code", "second-h1", "h3", "html-comment"],
)
def test_any_non_h1_preamble_text_is_refused(tmp_path: Path, wiki: Path, loose: str) -> None:
    head = "# Lemon Pistachio Loaf (imported)"
    raw_text = FULL_RAW.replace(_H1_RAW, f"{head}\n\n{loose}\n\n## Ingredients")
    assert raw_text != FULL_RAW
    out = _upsert(_raw(tmp_path, raw_text), wiki)
    assert out is not None and out.status == "refused" and out.reason == "unappliable"
    assert (wiki / f"{UID}.md").read_text(encoding="utf-8") == LINK_ONLY_PAGE


def test_whitespace_only_preamble_still_applies(tmp_path: Path, wiki: Path) -> None:
    head = "# Lemon Pistachio Loaf (imported)"
    raw_text = FULL_RAW.replace(_H1_RAW, f"{head}\n\n   \n\t\n\n## Ingredients")
    out = _upsert(_raw(tmp_path, raw_text), wiki)
    assert out is not None and out.status == "applied"


@pytest.mark.parametrize(
    "status_line",
    ["", "fetch_status: partial\n"],
    ids=["missing", "unranked"],
)
def test_missing_or_unranked_raw_rank_does_not_replace_sections(
    tmp_path: Path, wiki: Path, status_line: str
) -> None:
    _upsert(_raw(tmp_path, FULL_RAW), wiki)
    weak = (
        FULL_RAW.replace("fetch_status: full\n", status_line)
        .replace("- 200 g flour", "- 999 g mystery")
        .replace("- Lee swapped in almonds.", "- Weak raw note.")
        .replace("  - citrus", "  - citrus\n  - holiday")
    )
    assert "fetch_status: full" not in weak
    out = _upsert(_raw(tmp_path, weak, "r2.md"), wiki)
    assert out is not None and out.status == "applied"
    meta, body = _page(wiki)
    assert "200 g flour" in body and "999 g mystery" not in body
    assert "Weak raw note." in body and "holiday" in meta["tags"]
    assert meta["fetch_status"] == "full"


def test_unranked_page_value_does_not_gate_replacement(tmp_path: Path, wiki: Path) -> None:
    """The gate only protects a page that holds a ranked value."""
    (wiki / f"{UID}.md").write_text(
        LINK_ONLY_PAGE.replace("fetch_status: link-only\n", ""), encoding="utf-8"
    )
    out = _upsert(_raw(tmp_path, FULL_RAW.replace("fetch_status: full\n", "")), wiki)
    assert out is not None and out.status == "applied"
    assert "200 g flour" in _page(wiki)[1]


@pytest.mark.parametrize(
    ("opener", "inner", "closer"),
    [("````", "```", "````"), ("```", "~~~", "```"), ("~~~~", "~~~", "~~~~")],
    ids=["long-backtick-holds-short", "backtick-holds-tilde", "long-tilde-holds-short"],
)
def test_balanced_fence_holding_other_fence_applies(
    tmp_path: Path, wiki: Path, opener: str, inner: str, closer: str
) -> None:
    raw_text = FULL_RAW.replace(
        "3. Bake for 45 minutes.",
        f"{opener}\n{inner}\n## still code\n{closer}\n3. Bake for 45 minutes.",
    )
    out = _upsert(_raw(tmp_path, raw_text), wiki)
    assert out is not None and out.status == "applied"
    body = _page(wiki)[1]
    assert "## still code" in body and "## Notes" in body


def test_shorter_closer_does_not_close_a_longer_fence(tmp_path: Path, wiki: Path) -> None:
    raw_text = FULL_RAW.replace("3. Bake for 45 minutes.", "````\n```\n3. Bake for 45 minutes.")
    out = _upsert(_raw(tmp_path, raw_text), wiki)
    assert out is not None and out.status == "refused" and out.reason == "unappliable"


# --- review: unknown raw keys, keep_keys, refusal back-off -----------------------

_EXTRA = "servings: '8'\nemail_shape: link\nextra_note: x\n"


def test_unknown_raw_frontmatter_key_is_refused_naming_it(
    tmp_path: Path, wiki: Path, caplog: pytest.LogCaptureFixture
) -> None:
    raw_text = FULL_RAW.replace("servings: '8'\n", _EXTRA)
    with caplog.at_level(logging.WARNING):
        out = _upsert(_raw(tmp_path, raw_text), wiki)
    assert out is not None and out.status == "refused" and out.reason == "unappliable"
    assert "email_shape" in caplog.text and "extra_note" in caplog.text
    assert (wiki / f"{UID}.md").read_text(encoding="utf-8") == LINK_ONLY_PAGE


def test_unknown_raw_key_is_skipped_and_retained_through_process_one(
    tmp_path: Path, wiki: Path
) -> None:
    client = _no_llm()
    raw = _raw(tmp_path, FULL_RAW.replace("servings: '8'\n", _EXTRA))
    result = _run(raw, wiki, client)
    assert result.skipped == ["structured-upsert-unappliable"]
    assert result.retain_raw is True and client.mock_calls == []
    assert raw.path.exists()


def test_keep_keys_page_value_wins_and_raw_still_applies(tmp_path: Path, wiki: Path) -> None:
    # The raw carries shared_at 2026-11-08; the page's first-share date must stay.
    out = _upsert(_raw(tmp_path, FULL_RAW), wiki)
    assert out is not None and out.status == "applied"
    meta, body = _page(wiki)
    assert meta["shared_at"] == "2026-10-01"
    assert "200 g flour" in body and meta["fetch_status"] == "full"


def test_differing_raw_name_keeps_the_page_name(tmp_path: Path, wiki: Path) -> None:
    raw_text = FULL_RAW.replace("name: Lemon-Pistachio Loaf", "name: Other")
    out = _upsert(_raw(tmp_path, raw_text), wiki)
    assert out is not None and out.status == "applied"
    assert _page(wiki)[0]["name"] == "Lemon-Pistachio Loaf"


def test_config_keep_keys_strip_identity_keys() -> None:
    specs = resolve_structured_upsert(
        {"librarian": {"structured_upsert": {"recipe": {"keep_keys": ["uid", "shared_at"]}}}}
    )
    assert specs["recipe"].keep_keys == ("shared_at",)


def _refused_root(tmp_path: Path) -> tuple[Path, Path]:
    page = LINK_ONLY_PAGE.replace("type: recipe", "type: concept")
    root = _seed_root(tmp_path, page)
    raw_path = root / "raw" / "recipes" / "20261108T120000Z-aabbccdd.md"
    raw_path.write_text(FULL_RAW, encoding="utf-8")
    return root, raw_path


def _loop_run(root: Path) -> dict:
    from athenaeum.librarian import run

    stats: dict = {}
    run(
        raw_root=root / "raw",
        wiki_root=root / "wiki",
        knowledge_root=root,
        max_api_calls=100,
        max_runtime=0,
        out_run_stats=stats,
    )
    return stats


def _no_llm_loop(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    import anthropic as anthropic_mod

    client = MagicMock()
    client.messages.create.side_effect = AssertionError("LLM tier reached")
    monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kwargs: client)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
    monkeypatch.setenv("ATHENAEUM_STUCK_FILE_THRESHOLD", "2")
    monkeypatch.setenv("ATHENAEUM_STUCK_FILE_BACKOFF_BASE_SECONDS", "0")
    return client


def test_refused_raw_attempts_increment_and_threshold_marks_it_stuck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from athenaeum.librarian import _load_stuck_ledger

    client = _no_llm_loop(monkeypatch)
    root, raw_path = _refused_root(tmp_path)

    stats1 = _loop_run(root)
    ledger = _load_stuck_ledger(root / "wiki")
    ref = next(iter(ledger))
    assert ledger[ref]["failures"] == 1
    assert ledger[ref]["last_error"] == "structured-upsert-type-mismatch"
    assert stats1["stuck_files"] == []

    stats2 = _loop_run(root)  # a second consecutive refusal crosses the threshold
    assert _load_stuck_ledger(root / "wiki")[ref]["failures"] == 2
    assert [s["ref"] for s in stats2["stuck_files"]] == [ref]
    assert stats2["stuck_files"][0]["action"] == "tier0_structured_upsert"

    stats3 = _loop_run(root)  # now skipped without another attempt, still surfaced
    assert _load_stuck_ledger(root / "wiki")[ref]["failures"] == 2
    assert [s["ref"] for s in stats3["stuck_files"]] == [ref]
    assert raw_path.read_text(encoding="utf-8") == FULL_RAW  # never deleted
    assert client.messages.create.call_count == 0


def test_editing_a_refused_raw_resets_its_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from athenaeum.librarian import _load_stuck_ledger

    _no_llm_loop(monkeypatch)
    root, raw_path = _refused_root(tmp_path)
    _loop_run(root)
    raw_path.write_text(FULL_RAW + "\n- edited\n", encoding="utf-8")
    _loop_run(root)
    ledger = _load_stuck_ledger(root / "wiki")
    assert next(iter(ledger.values()))["failures"] == 1

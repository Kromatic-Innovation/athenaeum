# SPDX-License-Identifier: Apache-2.0
"""``recall_search`` must not crash on a YAML-ambiguous frontmatter tag
(issue athenaeum#1491).

YAML 1.1 parses certain unquoted scalars as non-str types: `9:00` folds to
the sexagesimal int 540, `true`/`no` fold to bool, `1.5` folds to float,
`2026-01-01` folds to a `datetime.date`. ``_recall_via_backend`` renders
``fm.get("tags")`` with ``", ".join(tags)``, which crashes with
``TypeError: sequence item 0: expected str instance, int found`` (etc.) the
moment any element is not already a str. The ``isinstance(tags, list)``
guard checks only the container, not its elements.

Every fixture here is written as REAL markdown with unquoted YAML and loaded
through the actual frontmatter parser -- a hand-built ``{"tags": [540]}``
dict would never exercise the parse step where the bug actually lives.

Test classes:

- ``TestTimeShapedTagDoesNotCrash`` -- AC1 (both ``fts5`` and ``keyword``
  backends) using the issue's own `9:00` reproduction, plus AC2 (the page is
  still ranked/rendered, tag shown in a readable -- if not literally
  reconstructed -- form).
- ``TestOtherYamlAmbiguousScalars`` -- pins the CLASS, not just the `9:00`
  instance: a bool-shaped (`true`) and a float-shaped (`1.5`) tag, each
  loaded from real YAML.
- ``TestAllStringTagsUnaffected`` -- byte-identical rendering for the
  ordinary all-str case (no regression in the common path).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.mcp_server import recall_search
from athenaeum.models import parse_frontmatter
from athenaeum.search import build_fts5_index


def _write_standup(wiki_root: Path, *, tags_block: str) -> Path:
    """Write the page from the issue's own reproduction, with *tags_block*
    substituted for the ``tags:`` frontmatter entry (unquoted, so real YAML
    parsing decides each element's type -- never a hand-built dict)."""
    wiki_root.mkdir(parents=True, exist_ok=True)
    path = wiki_root / "standup.md"
    path.write_text(
        "---\nuid: standup\ntype: meeting\nname: Standup\n"
        f"{tags_block}"
        "access: public\n---\n\n# Standup\n\nThe standup is at 9:00.\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def corpus(tmp_path: Path) -> tuple[Path, Path]:
    wiki = tmp_path / "wiki"
    cache = tmp_path / "cache"
    cache.mkdir()
    return wiki, cache


class TestTimeShapedTagDoesNotCrash:
    """AC1 + AC2: the issue's own `9:00` reproduction, both backends."""

    TAGS_BLOCK = "tags:\n  - 9:00\n  - meeting\n"

    def test_yaml_actually_parses_the_time_tag_as_int(self) -> None:
        """Sanity check on the premise: confirms the fixture below reaches
        the parser as a real int, not a str that merely looks like one."""
        text = f"---\n{self.TAGS_BLOCK}---\n\nbody\n"
        meta, _ = parse_frontmatter(text)
        assert meta["tags"] == [540, "meeting"]
        assert isinstance(meta["tags"][0], int)
        assert not isinstance(meta["tags"][0], str)

    def test_fts5_path_does_not_crash(self, corpus: tuple[Path, Path]) -> None:
        wiki, cache = corpus
        _write_standup(wiki, tags_block=self.TAGS_BLOCK)
        build_fts5_index(wiki, cache)

        result = recall_search(
            wiki, "what time is standup?", search_backend="fts5", cache_dir=cache
        )

        assert "Standup" in result
        # AC2: still rendered, tag present in readable (str) form.
        assert "**Tags:** 540, meeting" in result

    def test_keyword_path_does_not_crash(self, corpus: tuple[Path, Path]) -> None:
        wiki, cache = corpus
        _write_standup(wiki, tags_block=self.TAGS_BLOCK)

        result = recall_search(
            wiki, "what time is standup?", search_backend="keyword", cache_dir=cache
        )

        assert "Standup" in result
        assert "**Tags:** 540, meeting" in result


class TestOtherYamlAmbiguousScalars:
    """Pins the CLASS: any YAML-ambiguous scalar, not just times.

    `true` folds to bool, `1.5` folds to float -- both hit the same
    ``", ".join(tags)`` crash site as the int case above.
    """

    @pytest.mark.parametrize(
        ("tags_block", "expected_type", "expected_rendered"),
        [
            pytest.param(
                "tags:\n  - true\n  - meeting\n", bool, "True, meeting", id="bool"
            ),
            pytest.param(
                "tags:\n  - 1.5\n  - meeting\n", float, "1.5, meeting", id="float"
            ),
        ],
    )
    def test_backend_does_not_crash(
        self,
        corpus: tuple[Path, Path],
        tags_block: str,
        expected_type: type,
        expected_rendered: str,
    ) -> None:
        wiki, cache = corpus
        path = _write_standup(wiki, tags_block=tags_block)

        # Confirm the fixture reaches the parser as the claimed non-str type
        # (real YAML parse, not a hand-built dict).
        meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert isinstance(meta["tags"][0], expected_type)
        assert not isinstance(meta["tags"][0], str)

        build_fts5_index(wiki, cache)
        result = recall_search(
            wiki, "what time is standup?", search_backend="fts5", cache_dir=cache
        )

        assert "Standup" in result
        assert f"**Tags:** {expected_rendered}" in result


class TestAllStringTagsUnaffected:
    """The ordinary all-str case renders exactly as before -- no regression
    in the common path introduced by the per-element coercion."""

    def test_string_only_tags_render_unchanged(self, corpus: tuple[Path, Path]) -> None:
        wiki, cache = corpus
        _write_standup(wiki, tags_block="tags:\n  - standup\n  - meeting\n")
        build_fts5_index(wiki, cache)

        result = recall_search(
            wiki, "what time is standup?", search_backend="fts5", cache_dir=cache
        )

        assert "**Tags:** standup, meeting" in result

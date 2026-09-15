# SPDX-License-Identifier: Apache-2.0
"""The Mural template-board gate is gone; those boards classify like anything
else (issue athenaeum#1629).

athenaeum#1464 added a source-specific create gate: a ``mural-board-summary``
record whose YAML frontmatter title matched a reusable-worksheet pattern had
its classifications dropped and escalated rather than minting a page. The
operator set the principle on 2026-09-14 that athenaeum must not carry rules
specific to one source -- importing correctly is athenaeum-adapters' job -- and
the adapter now emits one record per clone family rather than one per copy
(Kromatic-Innovation/athenaeum-adapters#224), so the gate has no work left to
do.

The property these tests pin is a NEGATIVE one -- nothing intercepts the
classification list any more -- and a deleted test file asserts nothing, so the
guard has to be stated positively somewhere. Both transports are covered,
because the gate had a call site in each and a partial removal would leave one
path still gated. Verified against the pre-removal tree: both tests fail there
and pass here.

Deliberately name-free: the issue's first acceptance criterion is that a grep
for the deleted symbols over ``src/`` and ``tests/`` comes back empty, so this
file asserts the behaviour and never the identifiers.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.librarian import process_one
from athenaeum.models import ClassifiedEntity, EntityIndex, RawFile, TokenUsage

VALID_TYPES = ["person", "company", "concept", "reference"]
VALID_ACCESS = ["open", "internal", "confidential", "personal"]

#: The source the deleted gate keyed on, as a literal -- the constant that
#: used to hold it is gone.
MURAL_SOURCE = "mural-board-summary"

#: A title matching the deleted gate's default pattern
#: (``(?i)\b(?:template|example)\b|\bcopy of\b``) -- exactly the input that
#: used to be suppressed.
_TEMPLATE_TITLED_CONTENT = (
    "---\n"
    "name: TEMPLATE - Retro Exercise\n"
    "description: A reusable retro worksheet.\n"
    "source: external:mural:board-abc123\n"
    "---\n\n"
    "# TEMPLATE - Retro Exercise\n\n"
    "Instructions: copy this board for your team's retro.\n"
)


def _classified(name: str, *, observations: str = "") -> ClassifiedEntity:
    return ClassifiedEntity(
        name=name,
        entity_type="concept",
        tags=[],
        access="internal",
        is_new=True,
        existing_uid=None,
        observations=observations,
    )


def _raw(raw_dir: Path, content: str, filename: str = "board.md") -> RawFile:
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / filename
    path.write_text(content, encoding="utf-8")
    return RawFile(path=path, source=raw_dir.name, timestamp="", uuid8="")


def _write_client_returning(page_name: str) -> MagicMock:
    response = MagicMock()
    response.content = [
        MagicMock(
            text=f"# {page_name}\n\nInstructions for the retro.[^1]\n\n"
            f"[^1]: {MURAL_SOURCE}/board.md"
        )
    ]
    client = MagicMock()
    client.messages.create.return_value = response
    return client


class TestTemplateBoardNoLongerGated:
    def test_sync_transport_passes_the_classification_list_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        wiki.mkdir(parents=True)
        raw = _raw(knowledge / "raw" / MURAL_SOURCE, _TEMPLATE_TITLED_CONTENT)

        def _fake_tier2_classify(*_args: object, **_kwargs: object) -> list[ClassifiedEntity]:
            return [_classified("Retro Exercise", observations="Instructions for the retro.")]

        monkeypatch.setattr("athenaeum.librarian.tier2_classify", _fake_tier2_classify)

        classify_client = MagicMock()
        classify_client.messages.create.side_effect = AssertionError(
            "tier2_classify is monkeypatched — the real classify client must never be called"
        )

        result = process_one(
            raw,
            EntityIndex(wiki),
            wiki,
            classify_client,
            valid_types=VALID_TYPES,
            valid_tags=[],
            valid_access=VALID_ACCESS,
            usage=TokenUsage(),
            write_client=_write_client_returning("Retro Exercise"),
        )

        assert [e.name for e in result.created] == ["Retro Exercise"]
        assert result.escalated == []
        # Page filenames are ``<uid8>-<slug>``, so match the slug rather than
        # the entity name.
        page_names = [p.stem for p in wiki.glob("*.md") if not p.name.startswith("_")]
        assert len(page_names) == 1
        assert page_names[0].endswith("-retro-exercise")

    def test_batch_transport_passes_the_classification_list_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum.batch import process_batch_run

        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        wiki.mkdir(parents=True)
        raw = _raw(knowledge / "raw" / MURAL_SOURCE, _TEMPLATE_TITLED_CONTENT)

        def _fake_tier2_classify(*_args: object, **_kwargs: object) -> list[ClassifiedEntity]:
            return [_classified("Retro Exercise", observations="Instructions for the retro.")]

        monkeypatch.setattr("athenaeum.batch.tier2_classify", _fake_tier2_classify)

        classify_client = MagicMock()
        classify_client.messages.create.side_effect = AssertionError(
            "tier2_classify is monkeypatched — the real classify client must never be called"
        )

        result = process_batch_run(
            [raw],
            EntityIndex(wiki),
            wiki,
            classify_client,
            valid_types=VALID_TYPES,
            valid_tags=[],
            valid_access=VALID_ACCESS,
            usage=TokenUsage(),
            config=None,
            max_api_calls=100,
            write_client=_write_client_returning("Retro Exercise"),
            batch_classify=False,
            batch_write=False,
        )

        assert result.created == 1
        assert result.escalated == 0
        page_names = [p.stem for p in wiki.glob("*.md") if not p.name.startswith("_")]
        assert len(page_names) == 1
        assert page_names[0].endswith("-retro-exercise")

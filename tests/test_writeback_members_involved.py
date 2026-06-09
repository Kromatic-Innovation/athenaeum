# SPDX-License-Identifier: Apache-2.0
"""Issue #210 follow-up regression: write-back must resolve the true source
files from a ``Members involved:`` line even when the block ``source:`` header
points at a compiled wiki page and the refs are relative to ``raw/auto-memory``.

The original #210 lane only parsed ``**Member paths**:`` and resolved under
``raw/`` + ``wiki/`` — so on the REAL detector block shape (wiki-page source +
``Members involved:`` refs under ``raw/auto-memory/``) it resolved zero files
and edited nothing. These tests pin the real shape.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

from athenaeum.answers import (
    _extract_members_involved_refs,
    ingest_answers,
)  # noqa: PLC2701

_OFFENDING = "current primary venture"


def _dynamic_client() -> MagicMock:
    """A stub Anthropic client that rewrites each file it is shown.

    It parses the ``<file path="...">...</file>`` blocks out of the user
    message and returns an ``edits`` payload that strips the offending
    ``current primary venture`` phrase from each body — independent of how
    the resolved absolute path is formatted, so the test does not hardcode
    tmp_path string layout.
    """
    client = MagicMock()

    def _create(*_args: object, **kwargs: object) -> MagicMock:
        messages = kwargs["messages"]  # type: ignore[index]
        user_msg = messages[0]["content"]  # type: ignore[index]
        edits = []
        for m in re.finditer(
            r'<file path="([^"]+)">\n(.*?)\n</file>', user_msg, re.DOTALL
        ):
            path, body = m.group(1), m.group(2)
            if _OFFENDING in body:
                new_body = body.replace("; " + _OFFENDING, "")
                edits.append({"path": path, "changed": True, "new_body": new_body})
            else:
                edits.append({"path": path, "changed": False, "new_body": body})
        response = MagicMock()
        response.content = [MagicMock(text='{"edits": ' + _json(edits) + "}")]
        return response

    client.messages.create.side_effect = _create
    return client


def _json(obj: object) -> str:
    import json

    return json.dumps(obj)


def _make_knowledge(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Build a minimal knowledge dir; return (pending_path, raw_root, career)."""
    raw_root = tmp_path / "raw"
    wiki_root = tmp_path / "wiki"
    scope = "-Users-tristankromer-Code"
    career = raw_root / "auto-memory" / scope / "user_tristan_career.md"
    career.parent.mkdir(parents=True, exist_ok=True)
    career.write_text(
        "---\nname: career\ntype: user\n---\n"
        "- **Founder & CEO, Krobar.ai** — platform; current primary venture.\n"
        "- **Founder, Kromatic** — consulting.\n",
        encoding="utf-8",
    )
    wiki_root.mkdir(parents=True, exist_ok=True)
    pending = wiki_root / "_pending_questions.md"
    pending.write_text(
        "# Pending Questions\n\n"
        '## [2026-06-08] Entity: "tristan" (from wiki/auto-foo.md)\n'
        "- [x] Is Krobar the primary venture?\n"
        "**Conflict type**: factual\n"
        "**Description**: Member 1 frames Krobar as primary venture; "
        "Member 2 says co-equal.\n"
        "Passage 1: current primary venture.\n"
        "Passage 2: both are current; co-equal.\n"
        f"Members involved: {scope}/user_tristan_career.md\n"
        "\n"
        "Krobar and Kromatic are co-equal — stop framing Krobar as the "
        "primary venture.\n",
        encoding="utf-8",
    )
    return pending, raw_root, career


def test_extract_members_involved_refs() -> None:
    block = (
        "**Description**: x\n"
        "Members involved: -scope/a.md, -scope/b.md\n"
    )
    assert _extract_members_involved_refs(block) == ["-scope/a.md", "-scope/b.md"]


def test_ingest_edits_source_via_members_involved(tmp_path: Path) -> None:
    """The real detector shape: wiki-page source + Members involved ref under
    raw/auto-memory. ingest_answers with a client must mutate the source."""
    pending, raw_root, career = _make_knowledge(tmp_path)
    client = _dynamic_client()

    count = ingest_answers(pending, raw_root, client=client, config=None)

    assert count == 1
    edited = career.read_text(encoding="utf-8")
    assert "primary venture" not in edited
    assert "platform." in edited  # offending clause stripped, rest intact
    # frontmatter preserved
    assert edited.startswith("---\nname: career")
    # annotation marker NOT used when a concrete edit landed
    assert "> Krobar and Kromatic are co-equal" not in edited


def test_record_member_key_and_pair_text_from_full_block(tmp_path: Path) -> None:
    """Issue #216: the decision-log record must carry a non-empty member_key
    (from ``Members involved:``) and a pair_text that survives a ``**``-prefixed
    line between Passage 1 and Passage 2 (which truncates pq.description)."""
    import json

    from athenaeum.fingerprint import _member_key_str

    raw_root = tmp_path / "raw"
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir(parents=True, exist_ok=True)
    (raw_root).mkdir(parents=True, exist_ok=True)
    refs = ["-Users-x-Code/a.md", "-Users-x-Code/b.md"]
    pending = wiki_root / "_pending_questions.md"
    pending.write_text(
        "# Pending Questions\n\n"
        '## [2026-06-08] Entity: "x" (from wiki/auto-foo.md)\n'
        "- [x] Which framing is right?\n"
        "\n"
        "Both ventures are co-equal; drop the primary framing.\n"
        "\n"
        "**Conflict type**: factual\n"
        "**Description**: subtle framing conflict.\n"
        "Passage 1: Krobar is the current primary venture.\n"
        "**Founder & Lead, Kromatic** — intervening bold line truncates desc.\n"
        "Passage 2: Both are co-equal current ventures.\n"
        f"Members involved: {refs[0]}, {refs[1]}\n"
        "**Fingerprint**: abc123def4567890\n",
        encoding="utf-8",
    )

    # client=None: recording still happens (it is independent of write-back).
    ingest_answers(pending, raw_root, client=None, config=None)

    log_path = raw_root / "_resolved_contradictions.jsonl"
    assert log_path.exists()
    rec = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["member_key"] == _member_key_str(refs)
    assert rec["member_key"]  # non-empty
    assert rec["pair_text"]  # Passage 2 survived the ** truncation
    assert "\n##\n" in rec["pair_text"]


def test_ingest_without_client_falls_back_to_annotation(tmp_path: Path) -> None:
    """No client => no LLM edit; the offending claim survives (annotation path).
    Confirms the members-involved resolution still finds the file (so the
    annotation lands on the right source rather than nothing)."""
    pending, raw_root, career = _make_knowledge(tmp_path)

    count = ingest_answers(pending, raw_root, client=None, config=None)

    assert count == 1
    edited = career.read_text(encoding="utf-8")
    # The claim is NOT removed (no LLM), but the free-text ruling is annotated
    # onto the resolved source — proving member resolution worked.
    assert "primary venture" in edited
    assert "co-equal" in edited

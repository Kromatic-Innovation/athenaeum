# SPDX-License-Identifier: Apache-2.0
"""``athenaeum people`` is deprecated (issue athenaeum#966).

Now that athenaeum#964 (narrow ``recall`` by declared ``type``) and athenaeum#965
(the ``enumerate`` primitive) have shipped, every question ``people`` answers
is answerable through ``athenaeum enumerate --type person --where ...`` — see
``docs/recall-architecture.md``'s capability-parity table for the exact
per-flag mapping. This issue changes **zero behaviour**: it only adds a
migration notice.

These tests pin exactly that, on a small SYNTHETIC fixture corpus (public
repo — no client data, no real personal names, per athenaeum#966's own
constraint):

- ``TestDeprecationNotice`` — the notice fires on every call, goes to
  **stderr** only, and stdout stays byte-identical to what ``people``
  produced before this issue (a literal pre-change golden capture, not an
  approximation).
- ``TestCapabilityParity`` — for each row the capability-parity table marks
  reproducible (``--company``, ``--tag``/``--tier``, ``--title-regex``,
  ``--company-regex``, the default ``warm_score``-desc order, ``--limit``),
  the named ``enumerate`` expression returns the SAME set of pages as the
  ``people`` invocation it replaces, on this shared fixture. The two rows the
  table marks dropped (``--top-touch``'s computed composite sort,
  ``--format``) are deliberately not exercised here — there is no
  ``enumerate`` call to compare against.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from athenaeum.cli import main

# Four synthetic `type: person` pages plus one synthetic non-person page,
# unique `warm_score` values (no ties) so default-order comparisons are
# unambiguous. Company/title data is deliberately split across
# `current_*` and `linkedin_*_at_connect` so the `--company`/`--title-regex`
# fallback-field behavior is actually exercised.
_ALPHA = (
    "a1b2c3d4-example-page.md",
    "---\nuid: a1b2c3d4\ntype: person\nname: Example Person Alpha\n"
    "tags: [tier:warm-a, role:example]\ncurrent_company: Example Corp\n"
    "current_title: Example Director\nwarm_score: 90.0\n"
    "meeting_count_24mo: 10\nsent_count_24mo: 5\n"
    "last_touch: '2026-01-01'\n---\n\n# Example Person Alpha\n",
)
_BETA = (
    "b2c3d4e5-example-page.md",
    "---\nuid: b2c3d4e5\ntype: person\nname: Example Person Beta\n"
    "tags: [tier:warm-a]\nlinkedin_company_at_connect: Example Corp Holdings\n"
    "linkedin_position_at_connect: Example Manager\nwarm_score: 70.0\n"
    "meeting_count_24mo: 2\nsent_count_24mo: 50\n"
    "last_touch: '2026-01-02'\n---\n\n# Example Person Beta\n",
)
_GAMMA = (
    "c3d4e5f6-example-page.md",
    "---\nuid: c3d4e5f6\ntype: person\nname: Example Person Gamma\n"
    "tags: [tier:warm-b]\ncurrent_company: Other Example Co\n"
    "current_title: Example VP\nwarm_score: 40.0\n"
    "meeting_count_24mo: 1\nsent_count_24mo: 1\n"
    "last_touch: '2026-01-03'\n---\n\n# Example Person Gamma\n",
)
_DELTA = (
    "d4e5f6a7-example-page.md",
    "---\nuid: d4e5f6a7\ntype: person\nname: Example Person Delta\n"
    "tags: []\nwarm_score: 10.0\n---\n\n# Example Person Delta\n",
)
_NON_PERSON = (
    "e5f6a7b8-example-company.md",
    "---\nuid: e5f6a7b8\ntype: company\nname: Example Company Holdings\n"
    "---\n\n# Example Company Holdings\n",
)

# The exact `people --format table` output this fixture produced BEFORE
# athenaeum#966 (captured against unmodified `develop`). This issue must not
# change it by one byte.
_EXPECTED_TABLE = (
    "name                  title             company                score   touch  last_touch\n"
    "Example Person Alpha  Example Director  Example Corp             90.0     35  2026-01-01\n"
    "Example Person Beta   Example Manager   Example Corp Holdings    70.0     56  2026-01-02\n"
    "Example Person Gamma  Example VP        Other Example Co         40.0      4  2026-01-03\n"
    "Example Person Delta                                             10.0      0  \n"
    "\n"
    "4 match(es)\n"
)

# Same golden-capture discipline for `--format tsv`.
_EXPECTED_TSV = (
    "Example Person Alpha\tExample Director\tExample Corp\t90.0\t10\t5\t"
    "2026-01-01\ta1b2c3d4\ta1b2c3d4-example-page.md\n"
    "Example Person Beta\tExample Manager\tExample Corp Holdings\t70.0\t2\t50\t"
    "2026-01-02\tb2c3d4e5\tb2c3d4e5-example-page.md\n"
    "Example Person Gamma\tExample VP\tOther Example Co\t40.0\t1\t1\t"
    "2026-01-03\tc3d4e5f6\tc3d4e5f6-example-page.md\n"
    "Example Person Delta\t\t\t10.0\t0\t0\t\td4e5f6a7\td4e5f6a7-example-page.md\n"
)


@pytest.fixture
def fixture_knowledge(tmp_path: Path) -> Path:
    knowledge = tmp_path / "knowledge"
    wiki = knowledge / "wiki"
    wiki.mkdir(parents=True)
    for filename, content in (_ALPHA, _BETA, _GAMMA, _DELTA, _NON_PERSON):
        (wiki / filename).write_text(content, encoding="utf-8")
    return knowledge


def _enumerate_uids(
    knowledge: Path,
    tmp_path: Path,
    *,
    where: list[str] | None = None,
    sort_key: str = "warm_score",
    limit: int = 50,
    capsys: pytest.CaptureFixture[str],
) -> list[str]:
    """Run ``athenaeum enumerate`` and return the ordered ``uid`` list."""
    argv = [
        "enumerate",
        "--type",
        "person",
        "--path",
        str(knowledge),
        "--cache-dir",
        str(tmp_path / "cache"),
        "--sort",
        sort_key,
        "--limit",
        str(limit),
    ]
    for w in where or []:
        argv += ["--where", w]
    rc = main(argv)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    return [hit["uid"] for hit in payload["hits"]]


def _people_uids(
    knowledge: Path, argv_extra: list[str], *, capsys: pytest.CaptureFixture[str]
) -> list[str]:
    """Run ``athenaeum people --format tsv`` and return the ordered ``uid`` list."""
    rc = main(["people", "--path", str(knowledge), "--format", "tsv", *argv_extra])
    assert rc == 0
    out = capsys.readouterr().out
    return [line.split("\t")[7] for line in out.strip().splitlines()] if out.strip() else []


class TestDeprecationNotice:
    def test_notice_on_stderr_every_call(
        self, fixture_knowledge: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["people", "--path", str(fixture_knowledge)])
        captured = capsys.readouterr()

        assert rc == 0
        assert "[deprecated]" in captured.err
        assert "athenaeum#966" in captured.err
        assert "athenaeum enumerate" in captured.err
        assert "capability-parity table" in captured.err

    def test_notice_fires_even_on_error_path(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["people", "--path", str(tmp_path / "nope")])
        captured = capsys.readouterr()

        assert rc == 1
        assert "[deprecated]" in captured.err
        assert "Wiki root not found" in captured.err

    def test_stdout_table_is_byte_identical_to_pre_deprecation_capture(
        self, fixture_knowledge: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["people", "--path", str(fixture_knowledge)])
        captured = capsys.readouterr()

        assert rc == 0
        assert captured.out == _EXPECTED_TABLE
        assert "[deprecated]" not in captured.out

    def test_stdout_tsv_is_byte_identical_to_pre_deprecation_capture(
        self, fixture_knowledge: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["people", "--path", str(fixture_knowledge), "--format", "tsv"])
        captured = capsys.readouterr()

        assert rc == 0
        assert captured.out == _EXPECTED_TSV
        assert "[deprecated]" not in captured.out


class TestCapabilityParity:
    """Each reproducible parity-table row: same set of pages, `people` vs `enumerate`."""

    def test_default_order_matches(
        self, fixture_knowledge: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        legacy = _people_uids(fixture_knowledge, [], capsys=capsys)
        generalized = _enumerate_uids(fixture_knowledge, tmp_path, capsys=capsys)

        assert legacy == ["a1b2c3d4", "b2c3d4e5", "c3d4e5f6", "d4e5f6a7"]
        assert generalized == legacy

    def test_limit_matches(
        self, fixture_knowledge: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        legacy = _people_uids(fixture_knowledge, ["--limit", "2"], capsys=capsys)
        generalized = _enumerate_uids(fixture_knowledge, tmp_path, limit=2, capsys=capsys)

        assert legacy == ["a1b2c3d4", "b2c3d4e5"]
        assert generalized == legacy

    def test_company_and_matches(
        self, fixture_knowledge: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`--company Example --company Corp` (AND, repeatable substring)."""
        legacy = _people_uids(
            fixture_knowledge, ["--company", "Example", "--company", "Corp"], capsys=capsys
        )
        generalized = _enumerate_uids(
            fixture_knowledge,
            tmp_path,
            where=[
                "current_company,linkedin_company_at_connect:substring:Example",
                "current_company,linkedin_company_at_connect:substring:Corp",
            ],
            capsys=capsys,
        )

        assert set(legacy) == {"a1b2c3d4", "b2c3d4e5"}
        assert set(generalized) == set(legacy)

    def test_tier_shorthand_matches(
        self, fixture_knowledge: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        legacy = _people_uids(fixture_knowledge, ["--tier", "warm-a"], capsys=capsys)
        generalized = _enumerate_uids(
            fixture_knowledge, tmp_path, where=["tags:eq:tier:warm-a"], capsys=capsys
        )

        assert set(legacy) == {"a1b2c3d4", "b2c3d4e5"}
        assert set(generalized) == set(legacy)

    def test_title_regex_matches(
        self, fixture_knowledge: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        legacy = _people_uids(
            fixture_knowledge, ["--title-regex", "Director|Manager"], capsys=capsys
        )
        generalized = _enumerate_uids(
            fixture_knowledge,
            tmp_path,
            where=["current_title,linkedin_position_at_connect:regex:Director|Manager"],
            capsys=capsys,
        )

        assert set(legacy) == {"a1b2c3d4", "b2c3d4e5"}
        assert set(generalized) == set(legacy)

    def test_company_regex_matches(
        self, fixture_knowledge: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Deliberately un-anchored: `people`'s regex runs against a
        space-JOINED `current_company + " " + linkedin_company_at_connect`
        blob, so a `^`-anchored pattern can behave differently when the
        first field is empty and the second matches (the anchor lands on
        the joined space, not the real field start) — a presentation-level
        quirk of the legacy joined-blob implementation, not something this
        parity test exists to pin. An un-anchored pattern like this one
        sidesteps it and matches the common case the parity table
        documents."""
        legacy = _people_uids(
            fixture_knowledge, ["--company-regex", "Example Corp"], capsys=capsys
        )
        generalized = _enumerate_uids(
            fixture_knowledge,
            tmp_path,
            where=["current_company,linkedin_company_at_connect:regex:Example Corp"],
            capsys=capsys,
        )

        assert set(legacy) == {"a1b2c3d4", "b2c3d4e5"}
        assert set(generalized) == set(legacy)

    def test_non_person_pages_excluded_from_both(
        self, fixture_knowledge: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        legacy = _people_uids(fixture_knowledge, [], capsys=capsys)
        generalized = _enumerate_uids(fixture_knowledge, tmp_path, capsys=capsys)

        assert "e5f6a7b8" not in legacy
        assert "e5f6a7b8" not in generalized

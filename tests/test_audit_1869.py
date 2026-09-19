# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#1869 — code-side bare-person-stub override, its
precedence against the duplicate override, and spend-ceiling enforcement
in ``build_audit_report``.

Kept as a separate module from ``tests/test_audit.py``,
``tests/test_audit_1667.py``, and ``tests/test_audit_retirement_rules.py``
so this issue's acceptance criteria map to one file. All fixtures live
under ``tmp_path``; nothing here reads or writes a live knowledge store,
and no live LLM call is made anywhere in this module (see
``tests/evals/test_audit_retirement_eval.py`` for the layer that exercises
the real prompt against a real model).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import athenaeum.audit as audit_module
from athenaeum.audit import build_audit_report
from athenaeum.models import TokenUsage
from tests.conftest import FakeLLMClient, make_llm_response, make_llm_usage


def _page(root: Path, name: str, frontmatter: str, body: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")
    return path


@pytest.fixture
def wiki(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge" / "wiki"
    root.mkdir(parents=True)
    return root


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A ledger path isolated to tmp via ``ATHENAEUM_SPEND_LEDGER``, with
    every ambient ceiling env cleared so these tests are hermetic — mirrors
    ``tests/test_spend.py``'s ``ledger`` fixture.
    """
    path = tmp_path / "cache" / "spend.jsonl"
    monkeypatch.setenv("ATHENAEUM_SPEND_LEDGER", str(path))
    for var in (
        "ATHENAEUM_SPEND_MAX_TOKENS_PER_RUN",
        "ATHENAEUM_SPEND_MAX_TOKENS_PER_DAY",
        "ATHENAEUM_SPEND_MAX_USD_PER_RUN",
        "ATHENAEUM_SPEND_MAX_USD_PER_DAY",
        "ATHENAEUM_SPEND_LEDGER_ENABLED",
        "ATHENAEUM_SPEND_WEEKLY_TOKEN_LIMIT",
        "ATHENAEUM_SPEND_MAX_PCT_PER_DAY",
    ):
        monkeypatch.delenv(var, raising=False)
    return path


def _client(responder) -> FakeLLMClient:
    def wrapped(**params: Any) -> Any:
        return make_llm_response(responder(params), usage=make_llm_usage(10, 5))

    return FakeLLMClient(responder=wrapped)


# --- Bare-person-stub code override -----------------------------------------


class TestBarePersonStubOverride:
    """``_is_bare_person_stub`` forces ``retirement_candidate=False`` for a
    ``type: person`` page whose body is nothing but its own H1 heading (or
    empty) — regardless of what the model says. This is the belt-and-braces
    guarantee the prompt rewrite alone could not be measured against in
    this lane (no live LLM backend; see NOTES-FOR-PR.md).
    """

    def test_bare_h1_only_forced_false_even_when_model_says_true(self, wiki: Path) -> None:
        _page(
            wiki,
            "dana.md",
            "uid: dana1\ntype: person\nname: Dana Example\n",
            "# Dana Example\n",
        )

        def responder(params: dict[str, Any]) -> str:
            return json.dumps(
                {
                    "retirement_candidate": True,
                    "retirement_reason": "just a name, no claim",
                }
            )

        report = build_audit_report(wiki, client=_client(responder), model="m")
        [v] = report.verdicts
        assert v.retirement_candidate is False
        assert v.retirement_reason == ""

    def test_empty_body_forced_false_even_when_model_says_true(self, wiki: Path) -> None:
        _page(
            wiki,
            "empty.md",
            "uid: empty1\ntype: person\nname: Empty Person\n",
            "",
        )

        def responder(params: dict[str, Any]) -> str:
            return json.dumps({"retirement_candidate": True, "retirement_reason": "empty"})

        report = build_audit_report(wiki, client=_client(responder), model="m")
        [v] = report.verdicts
        assert v.retirement_candidate is False
        assert v.retirement_reason == ""

    def test_blank_lines_around_the_heading_still_count_as_bare(self, wiki: Path) -> None:
        _page(
            wiki,
            "spaced.md",
            "uid: spaced1\ntype: person\nname: Spaced Person\n",
            "\n\n# Spaced Person\n\n",
        )

        def responder(params: dict[str, Any]) -> str:
            return json.dumps({"retirement_candidate": True, "retirement_reason": "bare"})

        report = build_audit_report(wiki, client=_client(responder), model="m")
        [v] = report.verdicts
        assert v.retirement_candidate is False

    def test_name_plus_one_affiliation_line_is_two_lines_not_covered(self, wiki: Path) -> None:
        # A name plus a single affiliation line is TWO non-blank lines —
        # deliberately NOT covered by the code rule (stays the prompt's
        # job, per the placeholder exclusion in AUDIT_SYSTEM section 2).
        # The model's own verdict must pass through unmodified.
        _page(
            wiki,
            "alex.md",
            "uid: alex1\ntype: person\nname: Alex Sample\n",
            "# Alex Sample\nWorks at Example Widgets Ltd.\n",
        )

        def responder(params: dict[str, Any]) -> str:
            return json.dumps(
                {"retirement_candidate": True, "retirement_reason": "model's own call"}
            )

        report = build_audit_report(wiki, client=_client(responder), model="m")
        [v] = report.verdicts
        assert v.retirement_candidate is True
        assert v.retirement_reason == "model's own call"

    def test_non_person_type_with_bare_heading_not_covered(self, wiki: Path) -> None:
        # The code rule is person-only by design — a bare-heading company
        # page is not a "placeholder awaiting enrichment" in the same
        # sense and stays the prompt's call.
        _page(
            wiki,
            "co.md",
            "uid: co1\ntype: company\nname: Bare Co\n",
            "# Bare Co\n",
        )

        def responder(params: dict[str, Any]) -> str:
            return json.dumps(
                {"retirement_candidate": True, "retirement_reason": "model's own call"}
            )

        report = build_audit_report(wiki, client=_client(responder), model="m")
        [v] = report.verdicts
        assert v.retirement_candidate is True


# --- Precedence: duplicate override wins over the bare-stub override -------


class TestOverridePrecedence:
    def test_duplicate_override_wins_over_bare_stub_override(self, wiki: Path) -> None:
        """Two bare person stubs with byte-identical (whitespace-normalized)
        bodies are both bare stubs (would be forced False) AND duplicates of
        each other (would be forced True). Issue athenaeum#1869's ruling:
        the duplicate override wins — it is orthogonal, evidenced, and
        already shipped (issue athenaeum#1667) — so both pages must come
        back as retirement candidates with a "duplicate of" reason, not an
        empty stub-override reason.
        """
        _page(
            wiki,
            "twin1.md",
            "uid: twin1\ntype: person\nname: Case Twin\n",
            "# Case Twin\n",
        )
        _page(
            wiki,
            "twin2.md",
            "uid: twin2\ntype: person\nname: Case Twin\n",
            "# Case Twin\n",
        )

        def responder(params: dict[str, Any]) -> str:
            # The model itself says False — only the deterministic
            # duplicate rule should be able to flip this to True.
            return json.dumps({"retirement_candidate": False, "retirement_reason": ""})

        report = build_audit_report(wiki, client=_client(responder), model="m")
        assert len(report.verdicts) == 2
        for v in report.verdicts:
            assert v.retirement_candidate is True
            assert "duplicate of" in v.retirement_reason


# --- Spend ceiling enforcement -----------------------------------------------


class TestSpendCeilingEnforcement:
    """``build_audit_report`` must stop submitting further work once
    ``spend.ceiling_tripped`` reports the configured ceiling is reached,
    and must report WHERE it stopped (issue athenaeum#1869) — mirrors
    ``merge.py``'s C4-phase guard exactly (same ``log.error`` shape, same
    degrade-to-skip-not-raise contract).
    """

    def _three_pages(self, wiki: Path) -> None:
        for i in range(3):
            _page(
                wiki,
                f"page{i}.md",
                f"uid: page{i}\ntype: concept\nname: Concept {i}\n",
                f"Concept {i} states an independent claim about itself.\n",
            )

    def test_per_page_loop_stops_at_the_ceiling_and_records_where(
        self, wiki: Path, ledger: Path
    ) -> None:
        self._three_pages(wiki)
        # $1.00/M input, $5.00/M output for claude-haiku-4-5 — 100k/50k
        # tokens per page prices well over any near-zero ceiling, so page 1
        # trips the check made BEFORE page 2.
        client = FakeLLMClient(
            responder=lambda **params: make_llm_response(
                json.dumps({"retirement_candidate": False, "retirement_reason": ""}),
                usage=make_llm_usage(100_000, 50_000),
            )
        )
        config = {"spend": {"max_usd_per_run": 0.0000001}}

        report = build_audit_report(
            wiki, client=client, model="claude-haiku-4-5-20251001", config=config
        )

        assert len(report.audited) == 1
        assert len(report.skipped) == 2
        for _path, reason in report.skipped:
            assert "spend ceiling reached" in reason

        text = report.render_text()
        assert "skipped: 2" in text
        assert "SKIPPED" in text
        assert "spend ceiling reached" in text

    def test_ceiling_already_tripped_before_first_page_skips_everything(
        self, wiki: Path, ledger: Path
    ) -> None:
        self._three_pages(wiki)
        run_usage = TokenUsage()
        run_usage.add(100_000, 50_000, model="claude-haiku-4-5-20251001", knob="classify")
        client = FakeLLMClient(
            responder=lambda **params: make_llm_response(
                json.dumps({"retirement_candidate": False, "retirement_reason": ""}),
                usage=make_llm_usage(10, 5),
            )
        )
        config = {"spend": {"max_usd_per_run": 0.0000001}}

        report = build_audit_report(
            wiki,
            client=client,
            model="claude-haiku-4-5-20251001",
            config=config,
            run_usage=run_usage,
        )

        assert len(report.audited) == 0
        assert len(report.skipped) == 3
        assert report.llm_calls == 0

    def test_batch_path_checks_before_submit_and_never_calls_it(
        self, wiki: Path, ledger: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._three_pages(wiki)
        run_usage = TokenUsage()
        run_usage.add(100_000, 50_000, model="claude-haiku-4-5-20251001", knob="classify")
        config = {"spend": {"max_usd_per_run": 0.0000001}}

        calls: list[Any] = []

        def _fake_audit_pages_via_batch(*args: Any, **kwargs: Any) -> list[Any]:
            calls.append((args, kwargs))
            return []

        monkeypatch.setattr(audit_module, "audit_pages_via_batch", _fake_audit_pages_via_batch)

        report = build_audit_report(
            wiki,
            client=FakeLLMClient(text="{}"),
            model="claude-haiku-4-5-20251001",
            use_batch=True,
            config=config,
            run_usage=run_usage,
        )

        assert calls == []  # batch submit never called once the ceiling trips
        assert len(report.skipped) == 3
        for _path, reason in report.skipped:
            assert "spend ceiling reached" in reason

    def test_wiki_root_passed_to_ceiling_check_only_on_the_batch_path(
        self, wiki: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issue athenaeum#1147: outstanding batch reservations only exist on
        the batch submit path — ``ceiling_tripped``'s own docstring says
        *wiki_root* should be omitted everywhere else so behaviour matches
        every pre-#1147 / non-batch caller byte-for-byte.
        """
        self._three_pages(wiki)
        seen_wiki_roots: list[Any] = []

        def _fake_ceiling_tripped(usage: Any, *, provider: Any, **kwargs: Any) -> None:
            seen_wiki_roots.append(kwargs.get("wiki_root"))
            return None

        monkeypatch.setattr("athenaeum.spend.ceiling_tripped", _fake_ceiling_tripped)
        # The ceiling stub above always reports "not tripped", so the batch
        # submit path would otherwise proceed for real; stub it out too —
        # this test only cares what ceiling_tripped was called with.
        monkeypatch.setattr(
            audit_module, "audit_pages_via_batch", lambda *a, **k: []
        )

        client = FakeLLMClient(
            responder=lambda **params: make_llm_response(
                json.dumps({"retirement_candidate": False, "retirement_reason": ""}),
                usage=make_llm_usage(10, 5),
            )
        )

        seen_wiki_roots.clear()
        build_audit_report(wiki, client=client, model="m", use_batch=False)
        assert seen_wiki_roots
        assert all(w is None for w in seen_wiki_roots)

        seen_wiki_roots.clear()
        build_audit_report(wiki, client=client, model="m", use_batch=True)
        assert seen_wiki_roots == [wiki]

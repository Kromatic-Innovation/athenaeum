# SPDX-License-Identifier: Apache-2.0
"""Tests for intake-side sensitive-content screening (issue #320).

Covers the design-pass §7 acceptance criteria: the medical detection
catalogue and false-positive posture, the config resolution + precedence, the
`drop`-is-a-config-error guard, that `remember_write` stamps `access:` without
mutating the body, and the load-bearing §5 raw→wiki→recall propagation
(a screener-labeled medical page is withheld from a restricted recall while the
owner still receives it).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.config import resolve_screening
from athenaeum.mcp_server import recall_search, remember_write
from athenaeum.models import parse_frontmatter
from athenaeum.screening import (
    ScreeningConfigError,
    is_medical,
    more_restrictive,
    screen_intake,
)

LABEL_RESTRICT = {"medical": {"action": "label_restrict", "access": "personal"}}


# ---------------------------------------------------------------------------
# Detection catalogue + false-positive posture (design pass §1, §2, §7)
# ---------------------------------------------------------------------------


class TestMedicalDetection:
    @pytest.mark.parametrize(
        "content",
        [
            "Diagnosed with type 2 diabetes last week",  # framing + condition (HIGH)
            "My doctor prescribed sertraline for my depression",  # prescribed + depression
            "slept badly, headache, saw my doctor about it",  # MEDIUM + context marker
            "Undergoing chemotherapy at the moment",  # condition + framing
            "Her PTSD has been flaring up",  # mental-health clinical
            "blood pressure 150/95 at the checkup today",  # clinical vital w/ value
        ],
    )
    def test_restricts_clinical_content(self, content: str) -> None:
        assert is_medical(content) is True

    @pytest.mark.parametrize(
        "content",
        [
            "took ibuprofen for a headache",  # OTC + symptom, no HIGH, no context
            "The startup's burn rate is $40k/month",  # not medical at all
            "had a cold last week, feeling better now",  # transient symptom, no context
            "bought some melatonin to help sleep",  # OTC med, no context marker
            "",  # empty
        ],
    )
    def test_passes_casual_or_nonmedical(self, content: str) -> None:
        assert is_medical(content) is False

    def test_bare_medium_needs_context_marker(self) -> None:
        # A single MEDIUM signal alone does not restrict; the SAME signal plus
        # a personal-clinical-context marker does.
        assert is_medical("had a headache") is False
        assert is_medical("my doctor asked about the headache") is True

    def test_icd_code_needs_co_occurring_medical_term(self) -> None:
        # A bare ICD-shaped token must not restrict on its own (§1 item 6).
        assert is_medical("ticket E11 is assigned to the backend team") is False
        assert is_medical("coded as E11 for the diabetes follow-up") is True

    def test_dosage_of_non_otc_is_high_but_otc_is_not(self) -> None:
        assert is_medical("takes 50mg of sertraline daily") is True
        # OTC drug by the same dosage shape is only MEDIUM (no context → pass).
        assert is_medical("took 200mg of ibuprofen") is False


# ---------------------------------------------------------------------------
# Access stickiness helper
# ---------------------------------------------------------------------------


class TestMoreRestrictive:
    def test_never_downgrades(self) -> None:
        assert more_restrictive("open", "personal") == "personal"
        assert more_restrictive("personal", "open") == "personal"
        assert more_restrictive("internal", "confidential") == "confidential"

    def test_unknown_loses_to_real_level(self) -> None:
        assert more_restrictive("", "personal") == "personal"
        assert more_restrictive("bogus", "internal") == "internal"


# ---------------------------------------------------------------------------
# Config resolution + precedence (§7)
# ---------------------------------------------------------------------------


class TestResolveScreening:
    def test_default_is_off(self) -> None:
        assert resolve_screening({})["medical"]["action"] == "off"
        assert resolve_screening(None)["medical"]["action"] == "off"

    def test_yaml_sets_action_and_access(self) -> None:
        cfg = {"screening": {"medical": {"action": "label_restrict"}}}
        resolved = resolve_screening(cfg)["medical"]
        assert resolved["action"] == "label_restrict"
        assert resolved["access"] == "personal"  # default access

    def test_env_overrides_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_SCREEN_MEDICAL", "off")
        cfg = {"screening": {"medical": {"action": "label_restrict"}}}
        assert resolve_screening(cfg)["medical"]["action"] == "off"

    def test_drop_is_config_error(self) -> None:
        with pytest.raises(ScreeningConfigError):
            resolve_screening({"screening": {"medical": {"action": "drop"}}})

    def test_unknown_action_is_config_error(self) -> None:
        with pytest.raises(ScreeningConfigError):
            resolve_screening({"screening": {"medical": {"action": "nope"}}})

    def test_bad_access_level_is_config_error(self) -> None:
        cfg = {"screening": {"medical": {"action": "label_restrict", "access": "x"}}}
        with pytest.raises(ScreeningConfigError):
            resolve_screening(cfg)


# ---------------------------------------------------------------------------
# screen_intake action dispatch
# ---------------------------------------------------------------------------


class TestScreenIntake:
    def test_label_restrict_stamps_configured_access(self) -> None:
        assert screen_intake("Diagnosed with diabetes", LABEL_RESTRICT) == "personal"

    def test_off_or_none_stamps_nothing(self) -> None:
        assert screen_intake("Diagnosed with diabetes", None) is None
        assert screen_intake("Diagnosed with diabetes", {"medical": {"action": "off"}}) is None

    def test_non_medical_content_stamps_nothing(self) -> None:
        assert screen_intake("burn rate is high", LABEL_RESTRICT) is None


# ---------------------------------------------------------------------------
# remember_write integration — stamps access, never mutates the body (§4)
# ---------------------------------------------------------------------------


class TestRememberWriteScreening:
    def _raw(self, tmp_path: Path) -> Path:
        raw = tmp_path / "raw"
        raw.mkdir()
        return raw

    def test_medical_intake_is_labeled(self, tmp_path: Path) -> None:
        raw = self._raw(tmp_path)
        body = "Diagnosed with type 2 diabetes last week."
        result = remember_write(raw, body, screening=LABEL_RESTRICT)
        assert result.startswith("Saved to")
        text = Path(result[len("Saved to ") :].strip()).read_text()
        meta, parsed_body = parse_frontmatter(text)
        assert meta.get("access") == "personal"
        # Body bytes are byte-identical to the submitted content (§4/§7).
        assert body in text
        assert parsed_body.strip() == body

    def test_non_medical_intake_is_not_labeled(self, tmp_path: Path) -> None:
        raw = self._raw(tmp_path)
        result = remember_write(
            raw, "The burn rate is $40k/month.", screening=LABEL_RESTRICT
        )
        text = Path(result[len("Saved to ") :].strip()).read_text()
        meta, _ = parse_frontmatter(text)
        assert "access" not in meta

    def test_screening_off_leaves_intake_inert(self, tmp_path: Path) -> None:
        raw = self._raw(tmp_path)
        result = remember_write(
            raw, "Diagnosed with diabetes", screening={"medical": {"action": "off"}}
        )
        text = Path(result[len("Saved to ") :].strip()).read_text()
        meta, _ = parse_frontmatter(text)
        assert "access" not in meta

    def test_never_downgrades_caller_access(self, tmp_path: Path) -> None:
        raw = self._raw(tmp_path)
        # Caller explicitly stamped a weaker access in the content; the medical
        # screen must not weaken it further than personal.
        content = "---\naccess: open\n---\n\nDiagnosed with diabetes.\n"
        result = remember_write(raw, content, screening=LABEL_RESTRICT)
        text = Path(result[len("Saved to ") :].strip()).read_text()
        meta, _ = parse_frontmatter(text)
        assert meta.get("access") == "personal"


# ---------------------------------------------------------------------------
# §5 load-bearing propagation: raw → compiled wiki → recall withholds
# ---------------------------------------------------------------------------


class TestScreenerAccessPropagation:
    def test_sticky_access_survives_llm_compile_and_recall_withholds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A screener-set `access: personal` on unstructured medical intake must
        survive the Tier-2/3 LLM compile onto the wiki page — even when the LLM
        classifies a weaker access — and a restricted recall must then withhold
        the page while the owner still receives it.
        """
        import anthropic as anthropic_mod

        from athenaeum.librarian import process_one
        from athenaeum.models import EntityIndex, RawFile

        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()

        # Raw intake exactly as the screener would have written it: a medical
        # note stamped `access: personal`, but NO uid/type/name — so it does
        # NOT hit Tier-0 passthrough and must flow through the LLM tiers.
        raw_path = tmp_path / "raw" / "claude-session" / "20260709T120000Z-deadbeef.md"
        raw_path.parent.mkdir(parents=True)
        raw_content = (
            "---\n"
            "access: personal\n"
            "source: claude:inferred\n"
            "---\n\n"
            "Owner was diagnosed with type 2 diabetes and started metformin.\n"
        )
        raw_path.write_text(raw_content)
        raw = RawFile(
            path=raw_path,
            source="claude-session",
            timestamp="20260709T120000Z",
            uuid8="deadbeef",
        )

        # Mock LLM: Tier-2 classifies the entity with a DELIBERATELY WEAKER
        # access ("internal") to prove the screener label — not the LLM guess —
        # wins; Tier-3 writes the page body.
        classify_response = MagicMock()
        classify_response.content = [
            MagicMock(
                text=json.dumps(
                    [
                        {
                            "name": "Owner health status",
                            "entity_type": "reference",
                            "tags": [],
                            "access": "internal",
                            "observations": "Diagnosed with type 2 diabetes.",
                        }
                    ]
                )
            )
        ]
        create_response = MagicMock()
        create_response.content = [
            MagicMock(text="# Owner health status\n\nDiagnosed with type 2 diabetes.\n")
        ]
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [classify_response, create_response]
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kw: mock_client)

        result = process_one(
            raw,
            EntityIndex(wiki_root),
            wiki_root,
            mock_client,
            valid_types=["reference", "person"],
            valid_tags=[],
            valid_access=["open", "internal", "confidential", "personal"],
        )
        assert result.created, "expected a wiki page to be created"

        pages = list(wiki_root.glob("*.md"))
        assert len(pages) == 1
        page_meta, _ = parse_frontmatter(pages[0].read_text())
        # Propagation held: the LLM said "internal", the raw said "personal";
        # personal is authoritative and stuck.
        assert page_meta.get("access") == "personal"

        # Read-time gate (#312): a restricted caller is withheld the page; the
        # owner (caller_audience=None) still receives it.
        restricted = recall_search(
            wiki_root, "diabetes health status", caller_audience={"secondary"}
        )
        assert "Owner health status" not in restricted
        owner_view = recall_search(wiki_root, "diabetes health status")
        assert "Owner health status" in owner_view

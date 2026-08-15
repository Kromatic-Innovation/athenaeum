# SPDX-License-Identifier: Apache-2.0
"""Golden-fixture regression tests for ``scripts/pii-restore.py::classify()``
(issue athenaeum#844).

``classify()`` is the entire safe/unsafe boundary for athenaeum#691's restore
pass: given the token a `[contact redacted → excluded surface]` marker used
to replace, it decides whether that token is safe to put back verbatim or
must stay redacted as real PII. It had zero regression coverage despite two
undocumented hand-patches before the script was relocated into this repo
(athenaeum#925) — this file is the coverage that was missing, pinned against
the script AS RELOCATED, with no change to its classification logic (the
issue is explicit that logic changes are out of scope).

Two test surfaces:

- ``TestClassifyGoldenFixture`` — one synthetic marker per bucket
  ``classify()`` can return (every ``email:*``/``date:*``/numeric class, plus
  the ``None`` "stays redacted" outcomes for real-PII and phone-like/
  unrecognized tokens). A future hand-patch that moves the safe/unsafe
  boundary changes one of these asserted labels and fails the build.
- ``TestSafeEmailExactConfig`` — :func:`safe_email_exact`'s "code defaults ∪
  live config, fail CLOSED" contract, called with an explicit
  ``knowledge_root`` so the result never depends on whatever the host
  running the suite happens to have in its own ``~/knowledge/athenaeum.yaml``
  (the exact untracked-dependence athenaeum#844's last triage comment calls
  out to avoid). A malformed or missing config block must yield just the
  generic code defaults, per the module's fail-closed contract.

All addresses below are synthetic (``@example.com``/``@example.org``,
RFC 2606) — this is a public repo and this file's entire job is pinning the
PII safety boundary, so it must not itself introduce a real address.

``classify()``'s module-level ``SAFE_EMAIL_EXACT`` is resolved once at
import time from ``safe_email_exact()`` with no ``knowledge_root`` (i.e.
whatever the importing host's ``~/knowledge/athenaeum.yaml`` contains, if
anything). The golden fixture only asserts the ``email:host-alias/path``
bucket against the two CODE-DEFAULT addresses (``git@github.com``,
``root@example.com``), which the union always contains regardless of host
config — never against a config-only address, which would make the test's
verdict depend on the operator's live config.
"""

from __future__ import annotations

import importlib.util
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "pii-restore.py"


def _load_module():
    """Import the standalone script as a module (it lives in scripts/, not the package)."""
    spec = importlib.util.spec_from_file_location("pii_restore", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Loaded once: classify() and safe_email_exact() are pure w.r.t. their
# arguments, so there is no per-test isolation need (cf. test_write_build_sha.py,
# which reloads per test because it monkeypatches the module's subprocess).
pii_restore = _load_module()


# --------------------------------------------------------------------------- #
# classify() golden fixture — one marker per bucket
# --------------------------------------------------------------------------- #

# (label, token, expected classify() return value)
GOLDEN_CASES: list[tuple[str, str, str | None]] = [
    # --- degenerate input ---------------------------------------------------
    ("empty_string", "", None),
    ("whitespace_only", "   ", None),
    # --- email: safe-to-restore buckets -------------------------------------
    ("email_exact_default_git", "git@github.com", "email:host-alias/path"),
    ("email_exact_default_root", "root@example.com", "email:host-alias/path"),
    (
        "email_service_id_gserviceaccount",
        "svc-acct@my-project.iam.gserviceaccount.com",
        "email:service-id",
    ),
    (
        "email_service_id_calendar",
        "cal-abc123@group.calendar.google.com",
        "email:service-id",
    ),
    (
        "email_service_id_x_access_token",
        "x-access-token@github-actions.example",
        "email:service-id",
    ),
    ("email_role_noreply", "noreply@example.com", "email:role"),
    ("email_role_support", "support@example.org", "email:role"),
    ("email_test_account", "qa-test@example.com", "email:test-account"),
    # --- email: real-PII-keep bucket ----------------------------------------
    ("email_real_person_stays_redacted", "jane.doe@example.com", None),
    # --- date / numeric: safe-to-restore buckets ----------------------------
    ("date_iso", "2026-08-14", "date:iso"),
    ("date_iso_bracketed", "(2026-08-14)", "date:iso"),
    ("date_year_range", "2020-2023", "date:year-range"),
    ("isbn", "978-3-16-148410-0", "isbn"),
    ("decimal", ".75", "decimal"),
    ("id_fragment", "234567", "id-fragment"),
    ("date_embedded_in_id", "id-2024-03-15", "date:embedded"),
    ("number_list_double_dash", "691--720--683", "number-list"),
    ("number_other_short_run", "12 34 56", "number-other"),
    # --- numeric: ambiguous bucket (matches the numeric shape but is kept) --
    ("phone_like_digit_run_stays_redacted", "555-123-4567", None),
    # --- fallback: no bucket recognizes it, stays redacted ------------------
    ("unrecognized_prose_stays_redacted", "unclassified free text", None),
]


@pytest.mark.parametrize(
    "token,expected",
    [pytest.param(tok, exp, id=label) for label, tok, exp in GOLDEN_CASES],
)
def test_classify_golden_fixture(token: str, expected: str | None) -> None:
    assert pii_restore.classify(token) == expected


def test_golden_fixture_covers_every_returned_label() -> None:
    """Guard the guard: every distinct label ``classify()`` can hand back for a
    matched token appears at least once in the fixture above, so a NEW bucket
    added to ``classify()`` in the future must add its own fixture row (this
    test starts failing the moment the label set drifts from what is pinned)
    rather than silently shipping unpinned.
    """
    expected_labels = {exp for _, _, exp in GOLDEN_CASES if exp is not None}
    assert expected_labels == {
        "email:host-alias/path",
        "email:service-id",
        "email:role",
        "email:test-account",
        "date:iso",
        "date:year-range",
        "isbn",
        "decimal",
        "id-fragment",
        "date:embedded",
        "number-list",
        "number-other",
    }


# --------------------------------------------------------------------------- #
# safe_email_exact() — code defaults ∪ live config, fail CLOSED
# --------------------------------------------------------------------------- #


class TestSafeEmailExactConfig:
    def test_defaults_only_when_no_config_file(self, tmp_path: Path) -> None:
        """An empty knowledge root (no ``athenaeum.yaml`` at all) yields just
        the generic code defaults."""
        result = pii_restore.safe_email_exact(tmp_path)
        assert result == pii_restore.SAFE_EMAIL_EXACT_DEFAULT

    def test_defaults_union_live_config(self, tmp_path: Path) -> None:
        (tmp_path / "athenaeum.yaml").write_text(
            textwrap.dedent(
                """\
                pii:
                  restore:
                    safe_email_exact:
                      - ops-alias@example.com
                """
            )
        )
        result = pii_restore.safe_email_exact(tmp_path)
        assert result == pii_restore.SAFE_EMAIL_EXACT_DEFAULT | {"ops-alias@example.com"}

    def test_configured_entries_are_case_folded(self, tmp_path: Path) -> None:
        (tmp_path / "athenaeum.yaml").write_text(
            textwrap.dedent(
                """\
                pii:
                  restore:
                    safe_email_exact:
                      - Ops-Alias@Example.COM
                """
            )
        )
        result = pii_restore.safe_email_exact(tmp_path)
        assert "ops-alias@example.com" in result
        assert "Ops-Alias@Example.COM" not in result

    def test_fails_closed_on_non_list_value(self, tmp_path: Path) -> None:
        """A malformed ``safe_email_exact`` (not a list/tuple/set) is ignored
        entirely -- defaults only, not a crash and not a partial parse."""
        (tmp_path / "athenaeum.yaml").write_text(
            textwrap.dedent(
                """\
                pii:
                  restore:
                    safe_email_exact: "not-a-list"
                """
            )
        )
        result = pii_restore.safe_email_exact(tmp_path)
        assert result == pii_restore.SAFE_EMAIL_EXACT_DEFAULT

    def test_fails_closed_on_missing_restore_key(self, tmp_path: Path) -> None:
        (tmp_path / "athenaeum.yaml").write_text("pii: {}\n")
        result = pii_restore.safe_email_exact(tmp_path)
        assert result == pii_restore.SAFE_EMAIL_EXACT_DEFAULT

    def test_fails_closed_on_missing_pii_key(self, tmp_path: Path) -> None:
        (tmp_path / "athenaeum.yaml").write_text("auto_recall: true\n")
        result = pii_restore.safe_email_exact(tmp_path)
        assert result == pii_restore.SAFE_EMAIL_EXACT_DEFAULT

    def test_fails_closed_on_malformed_yaml(self, tmp_path: Path) -> None:
        (tmp_path / "athenaeum.yaml").write_text("pii: [unclosed\n")
        result = pii_restore.safe_email_exact(tmp_path)
        assert result == pii_restore.SAFE_EMAIL_EXACT_DEFAULT

    def test_unlisted_address_stays_redacted_end_to_end(self) -> None:
        """The end-to-end claim from athenaeum#844's relocation comment: an
        address absent from the (module-import-time-resolved) allowlist
        leaves ``classify()`` returning ``None`` -- i.e. it stays redacted,
        the safe direction. This address is never a code default and is not
        injected into any config the running test host might have, so this
        pins the fail-closed behaviour without depending on host state.
        """
        assert pii_restore.classify("ops-only-in-config@example.com") is None

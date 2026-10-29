# SPDX-License-Identifier: Apache-2.0
"""Collision logging on the contacts surface: one summary line per load (athenaeum#2188).

Every CLI ``recall`` loads :class:`~athenaeum.pii.ExcludedRecordIndex`, so a
per-key WARNING made each lookup print one line per collided key. These tests
pin the record COUNT at each level, which is what the change is about:

- a uid collision is a store defect: exactly one WARNING summary;
- an address-only collision is legitimate: exactly one INFO summary, no WARNING;
- a clean surface: no summary at all;
- the per-key lines survive at DEBUG, one per collided key, with the
  first-path name intact.

All fixtures are synthetic.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from athenaeum import pii
from athenaeum.pii import ExcludedRecordIndex, build_contact_record_uid_index


def _write(root: Path, name: str, uid: str, emails: list[str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"uid: {uid}"]
    if emails:
        lines.append("emails:")
        lines += [f"  - {e}" for e in emails]
    lines += ["---", ""]
    path = root / name
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _records(caplog: pytest.LogCaptureFixture, level: int) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == pii.log.name and r.levelno == level]


def _load(root: Path) -> None:
    ExcludedRecordIndex(root).uid_map()


class TestExcludedRecordIndexSummary:
    def test_uid_and_address_collisions_emit_one_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        root = tmp_path / "excluded"
        # Two uid collisions (alex, sam), and three shared addresses among them.
        _write(root, "alex.md", "alex", ["alex@example.org", "team@example.org"])
        _write(root, "alex-copy.md", "alex", ["alex@example.org"])
        _write(root, "sam.md", "sam", ["sam@example.org", "team@example.org"])
        _write(root, "sam-copy.md", "sam", ["sam@example.org"])
        _write(root, "kim.md", "kim", ["kim@example.org"])

        with caplog.at_level(logging.DEBUG, logger=pii.log.name):
            _load(root)

        warnings = _records(caplog, logging.WARNING)
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "2 uid(s) claimed by >1 record" in message
        assert "3 address(es) on >1 record" in message
        assert "athenaeum#2184" in message
        assert _records(caplog, logging.INFO) == []
        # One DEBUG line per collided key: 2 uids + 3 addresses.
        debug = _records(caplog, logging.DEBUG)
        assert len(debug) == 5
        uid_lines = [r.getMessage() for r in debug if r.getMessage().startswith("uid ")]
        assert len(uid_lines) == 2
        # Existing content survives, first-path name included.
        assert any("'alex'" in m and "(alex-copy.md)" in m for m in uid_lines)

    def test_address_only_collisions_are_info_not_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        root = tmp_path / "excluded"
        _write(root, "alex.md", "alex", ["home@example.org"])
        _write(root, "sam.md", "sam", ["home@example.org"])

        with caplog.at_level(logging.DEBUG, logger=pii.log.name):
            _load(root)

        assert _records(caplog, logging.WARNING) == []
        info = _records(caplog, logging.INFO)
        assert len(info) == 1
        assert "0 uid(s)" in info[0].getMessage()
        assert "1 address(es)" in info[0].getMessage()
        debug = _records(caplog, logging.DEBUG)
        assert len(debug) == 1
        assert "(alex.md)" in debug[0].getMessage()

    def test_clean_surface_logs_nothing(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        root = tmp_path / "excluded"
        _write(root, "alex.md", "alex", ["alex@example.org"])
        _write(root, "sam.md", "sam", ["sam@example.org"])

        with caplog.at_level(logging.DEBUG, logger=pii.log.name):
            _load(root)

        assert [r for r in caplog.records if r.name == pii.log.name] == []

    def test_summary_is_once_per_load_not_per_lookup(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        root = tmp_path / "excluded"
        _write(root, "alex.md", "alex", [])
        _write(root, "alex-copy.md", "alex", [])

        with caplog.at_level(logging.WARNING, logger=pii.log.name):
            index = ExcludedRecordIndex(root)
            for _ in range(3):
                index.by_uid("alex")
                index.by_identifier("nobody@example.org")

        assert len(_records(caplog, logging.WARNING)) == 1


class TestUidIndexSummary:
    def test_one_warning_and_per_uid_debug(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        root = tmp_path / "excluded"
        _write(root, "alex.md", "alex", [])
        _write(root, "alex-copy.md", "alex", [])
        _write(root, "sam.md", "sam", [])
        _write(root, "sam-copy.md", "sam", [])
        _write(root, "sam-third.md", "sam", [])

        with caplog.at_level(logging.DEBUG, logger=pii.log.name):
            index = build_contact_record_uid_index(root)

        assert set(index) == {"alex", "sam"}
        warnings = _records(caplog, logging.WARNING)
        assert len(warnings) == 1
        assert "2 uid(s) claimed by >1 record" in warnings[0].getMessage()
        # The scan never indexed addresses, so it must not claim a count.
        assert "address" not in warnings[0].getMessage()
        debug = _records(caplog, logging.DEBUG)
        assert len(debug) == 2
        assert any(
            "'sam'" in r.getMessage() and "3 contact records" in r.getMessage() for r in debug
        )

    def test_clean_surface_logs_nothing(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        root = tmp_path / "excluded"
        _write(root, "alex.md", "alex", [])

        with caplog.at_level(logging.DEBUG, logger=pii.log.name):
            build_contact_record_uid_index(root)

        assert [r for r in caplog.records if r.name == pii.log.name] == []

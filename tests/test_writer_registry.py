# SPDX-License-Identifier: Apache-2.0
"""Writer registry: labels, does not verify (issue athenaeum#2041)."""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.asserter_authority import compare_authority, declared_grants
from athenaeum.models import ASSERTER_TYPES, asserter_identity_key
from athenaeum.writer_registry import (
    WriterRegistryError,
    load_writer_registry,
    parse_writer_registry,
)

VALID = """\
version: 1
writers:
  - label: release-bot
    type: software_agent
    iss: https://idp.example.org
    sub: svc-release-bot
  - label: Pat Example
    type: person
    iss: https://idp.example.org
    provider_ids:
      entra_tid: tenant-1
      entra_oid: object-9
    sub: pairwise-ignored
"""


class TestLoadAndLookup:
    def test_load_and_lookup_by_identity_key(self, tmp_path: Path) -> None:
        path = tmp_path / "writer-registry.yaml"
        path.write_text(VALID, encoding="utf-8")
        registry = load_writer_registry(path)
        assert [w.label for w in registry.writers] == ["release-bot", "Pat Example"]

        bot = registry.lookup(("https://idp.example.org", "svc-release-bot"))
        assert bot is not None and bot.kind == "software_agent"
        # Entra branch: keyed on tenant + object id, `sub` ignored.
        pat = registry.lookup(("https://idp.example.org", "entra", "tenant-1", "object-9"))
        assert pat is not None and pat.label == "Pat Example"
        assert registry.lookup(("https://idp.example.org", "nobody")) is None
        assert registry.lookup(()) is None

    def test_key_is_the_models_derivation(self) -> None:
        registry = parse_writer_registry(VALID)
        asserter = {"iss": "https://idp.example.org", "sub": "svc-release-bot"}
        assert registry.writers[0].identity_key == asserter_identity_key(asserter)
        assert registry.lookup_asserter(asserter) is registry.writers[0]
        assert registry.lookup_asserter({}) is None

    def test_kinds_reuse_asserter_types(self) -> None:
        for kind in ASSERTER_TYPES:
            text = f"version: 1\nwriters:\n  - {{label: x, type: {kind}, iss: i, sub: s}}\n"
            assert parse_writer_registry(text).writers[0].kind == kind

    def test_missing_file_is_empty_registry(self, tmp_path: Path) -> None:
        registry = load_writer_registry(tmp_path / "absent.yaml")
        assert registry.writers == ()
        assert registry.lookup(("a", "b")) is None

    def test_empty_writers_list_is_valid(self) -> None:
        assert parse_writer_registry("version: 1\nwriters: []\n").writers == ()
        assert parse_writer_registry("version: 1\n").writers == ()


class TestMalformed:
    @pytest.mark.parametrize(
        ("text", "fragment"),
        [
            ("", "empty document"),
            ("- a\n- b\n", "must be a mapping"),
            ("version: [1\n", "invalid YAML"),
            ("writers: []\n", "unsupported version"),
            ("version: 2\nwriters: []\n", "unsupported version"),
            ("version: 1\nwriters: nope\n", "'writers' must be a list"),
            ("version: 1\nwriters:\n  - just-a-string\n", "writers[0] must be a mapping"),
            ("version: 1\nwriters:\n  - {type: person, iss: i, sub: s}\n", "'label'"),
            ("version: 1\nwriters:\n  - {label: x, iss: i, sub: s}\n", "'type'"),
            (
                "version: 1\nwriters:\n  - {label: x, type: robot, iss: i, sub: s}\n",
                "is not one of",
            ),
            (
                "version: 1\nwriters:\n  - {label: x, type: person, iss: i}\n",
                "no derivable identity key",
            ),
            (
                "version: 1\nwriters:\n  - {label: x, type: person, iss: i, sub: s}\n"
                "  - {label: x, type: person, iss: j, sub: t}\n",
                "duplicate writer label",
            ),
            (
                "version: 1\nwriters:\n  - {label: x, type: person, iss: i, sub: s}\n"
                "  - {label: y, type: person, iss: i, sub: s}\n",
                "duplicate identity key",
            ),
        ],
    )
    def test_each_defect_raises_a_named_error(self, text: str, fragment: str) -> None:
        with pytest.raises(
            WriterRegistryError, match=fragment.replace("[", r"\[").replace("]", r"\]")
        ):
            parse_writer_registry(text)

    def test_malformed_file_on_disk_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "writer-registry.yaml"
        path.write_text("version: 9\n", encoding="utf-8")
        with pytest.raises(WriterRegistryError):
            load_writer_registry(path)


class TestPathResolution:
    def test_precedence_env_yaml_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum.config import resolve_writer_registry_path

        monkeypatch.delenv("ATHENAEUM_WRITER_REGISTRY", raising=False)
        assert resolve_writer_registry_path(tmp_path) == tmp_path / "writer-registry.yaml"
        cfg = {"librarian": {"writer_registry_path": "sub/w.yaml"}}
        assert resolve_writer_registry_path(tmp_path, cfg) == tmp_path / "sub" / "w.yaml"
        absolute = tmp_path / "elsewhere.yaml"
        cfg_abs = {"librarian": {"writer_registry_path": str(absolute)}}
        assert resolve_writer_registry_path(tmp_path, cfg_abs) == absolute
        monkeypatch.setenv("ATHENAEUM_WRITER_REGISTRY", str(tmp_path / "env.yaml"))
        assert resolve_writer_registry_path(tmp_path, cfg) == tmp_path / "env.yaml"


class TestLabelsNeverConferAuthority:
    def test_registered_writer_with_forged_grants_is_none_declared_when_enforced(self) -> None:
        registry = parse_writer_registry(VALID)
        forged = {
            "asserter": {
                "iss": "https://idp.example.org",
                "sub": "svc-release-bot",
                "grants": ["admin"],
            }
        }
        other = {"asserter": {"iss": "https://idp.example.org", "sub": "x", "grants": ["reader"]}}
        assert registry.lookup_asserter(forged["asserter"]) is not None  # registered
        assert declared_grants(forged, writer_identity_enforced=True) == frozenset()
        assert (
            compare_authority(forged, other, writer_identity_enforced=True)
            == compare_authority({}, {})
            == "equal"
        )

    def test_nothing_outside_the_registry_module_reads_it(self) -> None:
        src = Path(__file__).resolve().parents[1] / "src" / "athenaeum"
        offenders = [
            p.name
            for p in src.glob("*.py")
            if p.name not in {"writer_registry.py", "config.py"}
            and "writer_registry" in p.read_text(encoding="utf-8")
        ]
        assert offenders == []

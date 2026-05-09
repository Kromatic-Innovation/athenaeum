"""Tests for `athenaeum init --with-templates` (issue #89)."""

from __future__ import annotations

from pathlib import Path

import yaml

from athenaeum.cli import main
from athenaeum.init import _TEMPLATE_FILES, copy_templates


def _split_frontmatter(text: str) -> tuple[str, str]:
    assert text.startswith("---\n"), "missing frontmatter"
    rest = text[4:]
    end = rest.index("\n---")
    return rest[:end], rest[end + 4 :]


def test_with_templates_creates_five_files(tmp_path: Path) -> None:
    target = tmp_path / "knowledge"
    exit_code = main(["init", "--path", str(target), "--with-templates"])
    assert exit_code == 0
    dest = target / "templates"
    for fname in _TEMPLATE_FILES:
        assert (dest / fname).is_file(), f"missing template: {fname}"
    assert len(list(dest.glob("*.md"))) == 5


def test_each_template_parses_yaml(tmp_path: Path) -> None:
    dest = tmp_path / "templates"
    copy_templates(dest)
    for fname in _TEMPLATE_FILES:
        text = (dest / fname).read_text(encoding="utf-8")
        fm, _ = _split_frontmatter(text)
        # yaml.safe_load must accept the frontmatter without error.
        meta = yaml.safe_load(fm)
        assert isinstance(meta, dict), f"frontmatter not a dict: {fname}"


def test_template_type_matches_basename(tmp_path: Path) -> None:
    dest = tmp_path / "templates"
    copy_templates(dest)
    for fname in _TEMPLATE_FILES:
        expected_type = fname[:-3]  # strip ".md"
        text = (dest / fname).read_text(encoding="utf-8")
        fm, _ = _split_frontmatter(text)
        meta = yaml.safe_load(fm)
        assert (
            meta.get("type") == expected_type
        ), f"{fname}: type={meta.get('type')!r} != {expected_type!r}"


def test_template_includes_source_and_field_sources_with_comments(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "templates"
    copy_templates(dest)
    for fname in _TEMPLATE_FILES:
        raw = (dest / fname).read_text(encoding="utf-8")
        assert "source:" in raw, f"{fname}: no source: key"
        assert "field_sources:" in raw, f"{fname}: no field_sources: key"
        # Explanatory comments preserved (string-contains).
        assert "# `source:`" in raw, f"{fname}: missing source: comment"
        assert "# `field_sources:`" in raw, f"{fname}: missing field_sources: comment"
        # Both scalar and structured forms documented.
        assert "<type>:<ref>" in raw, f"{fname}: scalar form not documented"
        assert (
            "type:" in raw and "ref:" in raw
        ), f"{fname}: structured form not documented"


def test_rerun_without_force_is_idempotent(tmp_path: Path) -> None:
    dest = tmp_path / "templates"
    written1, skipped1 = copy_templates(dest)
    assert sorted(written1) == sorted(_TEMPLATE_FILES)
    assert skipped1 == []

    # User edits a template.
    user_edit = "---\nuid: my-edit\n---\n\nuser content\n"
    (dest / "person.md").write_text(user_edit, encoding="utf-8")

    written2, skipped2 = copy_templates(dest)
    assert written2 == []
    assert sorted(skipped2) == sorted(_TEMPLATE_FILES)
    assert (dest / "person.md").read_text(encoding="utf-8") == user_edit


def test_force_overwrites_cleanly(tmp_path: Path) -> None:
    dest = tmp_path / "templates"
    copy_templates(dest)

    (dest / "person.md").write_text("custom\n", encoding="utf-8")
    written, skipped = copy_templates(dest, force=True)
    assert sorted(written) == sorted(_TEMPLATE_FILES)
    assert skipped == []
    # Restored to bundled content.
    text = (dest / "person.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "type: person" in text


def test_cli_templates_dest_override(tmp_path: Path) -> None:
    target = tmp_path / "knowledge"
    custom_dest = tmp_path / "elsewhere" / "tpls"
    exit_code = main(
        [
            "init",
            "--path",
            str(target),
            "--with-templates",
            "--templates-dest",
            str(custom_dest),
        ]
    )
    assert exit_code == 0
    for fname in _TEMPLATE_FILES:
        assert (custom_dest / fname).is_file()
    # Default location not used.
    assert not (target / "templates").exists()


def test_init_without_flag_does_not_copy_templates(tmp_path: Path) -> None:
    target = tmp_path / "knowledge"
    exit_code = main(["init", "--path", str(target)])
    assert exit_code == 0
    assert not (target / "templates").exists()

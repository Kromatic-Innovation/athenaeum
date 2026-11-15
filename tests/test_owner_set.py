# SPDX-License-Identifier: Apache-2.0
"""``athenaeum owner set`` / ``init --owner-name`` (issue athenaeum#2652)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from athenaeum.cli import main
from athenaeum.config import load_config, resolve_owner
from athenaeum.models import parse_frontmatter
from athenaeum.owner_page import set_owner


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for var, val in (
        ("GIT_AUTHOR_NAME", "T"),
        ("GIT_AUTHOR_EMAIL", "t@example.com"),
        ("GIT_COMMITTER_NAME", "T"),
        ("GIT_COMMITTER_EMAIL", "t@example.com"),
    ):
        monkeypatch.setenv(var, val)


@pytest.fixture
def kb(tmp_path: Path) -> Path:
    root = tmp_path / "kb"
    assert main(["init", "--path", str(root)]) == 0
    return root


def _pages(root: Path) -> list[Path]:
    return sorted(p for p in (root / "wiki").glob("*.md") if p.name != "_index.md")


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in [root / "athenaeum.yaml", *_pages(root)]
    }


def test_owner_set_creates_page_and_config(kb: Path) -> None:
    assert main(["owner", "set", "--path", str(kb), "--name", "Ada Example"]) == 0
    (page,) = _pages(kb)
    meta, _ = parse_frontmatter(page.read_text(encoding="utf-8"))
    owner = resolve_owner(load_config(kb))
    assert owner is not None
    assert meta["uid"] == owner["uid"]
    assert meta["type"] == "person"
    assert meta["name"] == "Ada Example"
    assert owner["aliases"] == ["Ada Example"]


def test_owner_page_is_personal_and_has_no_contact_data(kb: Path) -> None:
    set_owner(kb, "Ada Example")
    (page,) = _pages(kb)
    meta, _ = parse_frontmatter(page.read_text(encoding="utf-8"))
    assert meta["access"] == "personal"
    for key in ("emails", "email", "phones", "phone", "alt_emails"):
        assert key not in meta
    assert not (resolve_owner(load_config(kb)) or {}).get("emails")


def test_second_run_changes_nothing(kb: Path) -> None:
    set_owner(kb, "Ada Example", ["A. Example"])
    before = _snapshot(kb)
    result = set_owner(kb, "Ada Example", ["A. Example"])
    assert not result.changed
    assert _snapshot(kb) == before
    assert len(_pages(kb)) == 1


def test_new_alias_adds_only_the_alias(kb: Path) -> None:
    set_owner(kb, "Ada Example")
    first_uid = resolve_owner(load_config(kb))["uid"]  # type: ignore[index]
    (page,) = _pages(kb)
    set_owner(kb, "Ada Example", ["Ada B. Example"])
    assert _pages(kb) == [page]
    owner = resolve_owner(load_config(kb))
    assert owner is not None
    assert owner["uid"] == first_uid
    assert owner["aliases"] == ["Ada Example", "Ada B. Example"]
    meta, _ = parse_frontmatter(page.read_text(encoding="utf-8"))
    assert meta["aliases"] == ["Ada B. Example"]
    assert meta["access"] == "personal"


def test_comments_and_other_keys_preserved(kb: Path) -> None:
    cfg = kb / "athenaeum.yaml"
    before = cfg.read_text(encoding="utf-8")
    assert "# owner:" in before  # the shipped commented example
    set_owner(kb, "Ada Example")
    after = cfg.read_text(encoding="utf-8")
    assert after.startswith(before.rstrip("\n"))
    assert "# owner:" in after
    assert load_config(kb)["owner"]["uid"]

    # An existing live block keeps its other keys; comments outside survive.
    cfg.write_text(
        "# top comment\nowner:\n  google_contact: people/c1\n  emails:\n    - ada@example.com\n"
        "\n# trailing comment\nother: 1\n",
        encoding="utf-8",
    )
    set_owner(kb, "Ada Example", ["Countess Example"])
    text = cfg.read_text(encoding="utf-8")
    assert "# top comment" in text and "# trailing comment" in text
    parsed = yaml.safe_load(text)
    assert parsed["other"] == 1
    assert parsed["owner"]["google_contact"] == "people/c1"
    assert parsed["owner"]["emails"] == ["ada@example.com"]
    assert parsed["owner"]["aliases"] == ["Ada Example", "Countess Example"]


def test_existing_uid_is_reused(kb: Path) -> None:
    (kb / "athenaeum.yaml").write_text("owner:\n  uid: abcd1234\n", encoding="utf-8")
    set_owner(kb, "Ada Example")
    assert [p.name for p in _pages(kb)] == ["abcd1234-ada-example.md"]
    assert resolve_owner(load_config(kb))["uid"] == "abcd1234"  # type: ignore[index]


def test_single_word_alias_warns(kb: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["owner", "set", "--path", str(kb), "--name", "Ada Example", "--alias", "ada"]) == 0
    err = capsys.readouterr().err
    assert "'ada'" in err
    assert "single word" in err and "ignores" in err
    # Still recorded: it still matches an explicit handle field.
    assert "ada" in resolve_owner(load_config(kb))["aliases"]  # type: ignore[index]


def test_full_name_alias_does_not_warn(kb: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main(["owner", "set", "--path", str(kb), "--name", "Ada Example", "--alias", "A. Example"])
    assert "warning" not in capsys.readouterr().err


def test_requires_initialised_root(tmp_path: Path) -> None:
    assert main(["owner", "set", "--path", str(tmp_path / "nope"), "--name", "Ada Example"]) == 1


def test_init_owner_name_uses_same_path(tmp_path: Path) -> None:
    root = tmp_path / "kb"
    assert main(["init", "--path", str(root), "--owner-name", "Ada Example"]) == 0
    owner = resolve_owner(load_config(root))
    assert owner is not None and owner["aliases"] == ["Ada Example"]
    (page,) = _pages(root)
    assert page.name.startswith(owner["uid"] + "-")
    before = _snapshot(root)
    assert main(["owner", "set", "--path", str(root), "--name", "Ada Example"]) == 0
    assert _snapshot(root) == before
    # init is idempotent with the flag too.
    assert main(["init", "--path", str(root), "--owner-name", "Ada Example"]) == 0
    assert _snapshot(root) == before


def test_no_owner_configured_is_unchanged(tmp_path: Path) -> None:
    root = tmp_path / "kb"
    assert main(["init", "--path", str(root)]) == 0
    assert _pages(root) == []
    assert resolve_owner(load_config(root)) is None
    text = (root / "athenaeum.yaml").read_text(encoding="utf-8")
    assert yaml.safe_load(text).get("owner") is None

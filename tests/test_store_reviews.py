# SPDX-License-Identifier: Apache-2.0
"""The ``reviews`` block: mounted source, read grant, due, cursor (athenaeum#2992)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from athenaeum.store_access import StoreDeclaration, load_store_declaration
from athenaeum.store_reviews import (
    BAD_INTERVAL,
    NO_GRANT,
    NOT_MOUNTED,
    RUNNABLE,
    ReviewRefused,
    evaluate_reviews,
    load_review_runs,
    parse_every,
    record_review_run,
    state_path,
)

ISS = "https://git.example.net"
ME = {"iss": ISS, "sub": "me"}
TARGET = "st_tttttttttttt"
SRC = "st_ssssssssssss"
OTHER_SRC = "st_oooooooooooo"
NOW = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)


def _declare(root: Path, store_id: str, roles: dict, groups: dict | None = None,
             reviews: list | None = None) -> None:
    (root / "_store").mkdir(parents=True, exist_ok=True)
    data: dict = {"version": 1, "store_id": store_id, "roles": roles}
    if groups is not None:
        data["groups"] = groups
    if reviews is not None:
        data["reviews"] = reviews
    (root / "_store" / "access.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def _review(source: str = SRC, every: str = "7d", name: str = "weekly") -> dict:
    return {"name": name, "source": source, "every": every, "criteria": "_store/c.md"}


def _target(tmp_path: Path, reviews: list) -> StoreDeclaration:
    root = tmp_path / "target"
    _declare(root, TARGET, {"c": {"rights": ["read", "compile"], "members": [ME]}}, reviews=reviews)
    decl = load_store_declaration(root)
    assert isinstance(decl, StoreDeclaration)
    return decl


def _source(tmp_path: Path, roles: dict, groups: dict | None = None) -> dict[str, Path]:
    root = tmp_path / "source"
    _declare(root, SRC, roles, groups)
    return {SRC: root}


def _readers(who: object) -> dict:
    return {"r": {"rights": ["read"], "members": [who]}}


@pytest.fixture
def kr(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge"
    root.mkdir()
    return root


def test_source_found_among_mounts_and_runnable(tmp_path: Path, kr: Path) -> None:
    target = _target(tmp_path, [_review()])
    mounts = _source(tmp_path, _readers(ME))
    (status,) = evaluate_reviews(target, mounts, ME, kr, NOW)
    assert status.state == RUNNABLE
    assert status.review.name == "weekly"


def test_unmounted_source_is_reported(tmp_path: Path, kr: Path) -> None:
    target = _target(tmp_path, [_review(source=OTHER_SRC)])
    mounts = _source(tmp_path, _readers(ME))
    (status,) = evaluate_reviews(target, mounts, ME, kr, NOW)
    assert status.state == NOT_MOUNTED
    assert not status.should_run


def test_no_read_grant_skips_even_when_clone_is_on_disk(tmp_path: Path, kr: Path) -> None:
    target = _target(tmp_path, [_review()])
    mounts = _source(tmp_path, _readers({"iss": ISS, "sub": "someone-else"}))
    assert mounts[SRC].is_dir()
    (status,) = evaluate_reviews(target, mounts, ME, kr, NOW)
    assert status.state == NO_GRANT
    assert status.due  # due is computed, but it will not run
    assert not status.should_run


def test_invalid_source_declaration_grants_nothing(tmp_path: Path, kr: Path) -> None:
    target = _target(tmp_path, [_review()])
    mounts = _source(tmp_path, _readers(ME))
    (mounts[SRC] / "_store" / "access.yaml").write_text("version: 1\nstore_id: nope\n")
    (status,) = evaluate_reviews(target, mounts, ME, kr, NOW)
    assert status.state == NO_GRANT


def test_counter_example_group_grant_for_a_different_group_never_advances_cursor(
    tmp_path: Path, kr: Path
) -> None:
    target = _target(tmp_path, [_review()])
    other = {"iss": ISS, "sub": "leader"}
    mounts = _source(
        tmp_path,
        _readers("group:field-team"),
        groups={"field-team": [other], "leadership": [ME]},
    )
    (status,) = evaluate_reviews(target, mounts, ME, kr, NOW)
    assert status.state == NO_GRANT
    with pytest.raises(ReviewRefused):
        record_review_run(kr, TARGET, status, "rev1", {"pages": 3}, NOW)
    assert load_review_runs(kr, TARGET) == []
    assert not state_path(kr, TARGET).exists()


def test_group_grant_to_the_owners_group_runs(tmp_path: Path, kr: Path) -> None:
    target = _target(tmp_path, [_review()])
    mounts = _source(tmp_path, _readers("group:leadership"), groups={"leadership": [ME]})
    (status,) = evaluate_reviews(target, mounts, ME, kr, NOW)
    assert status.state == RUNNABLE


def test_no_previous_run_is_due(tmp_path: Path, kr: Path) -> None:
    target = _target(tmp_path, [_review()])
    (status,) = evaluate_reviews(target, _source(tmp_path, _readers(ME)), ME, kr, NOW)
    assert status.due and status.should_run and status.cursor is None


def test_due_follows_every_and_last_run(tmp_path: Path, kr: Path) -> None:
    target = _target(tmp_path, [_review(every="7d")])
    mounts = _source(tmp_path, _readers(ME))
    (first,) = evaluate_reviews(target, mounts, ME, kr, NOW)
    record_review_run(kr, TARGET, first, "rev1", {"pages": 2}, NOW)

    (soon,) = evaluate_reviews(target, mounts, ME, kr, NOW + timedelta(days=6, hours=23))
    assert not soon.due and not soon.should_run
    (later,) = evaluate_reviews(target, mounts, ME, kr, NOW + timedelta(days=7))
    assert later.due and later.should_run


def test_cursor_and_run_record_kept_without_source_text(tmp_path: Path, kr: Path) -> None:
    target = _target(tmp_path, [_review()])
    mounts = _source(tmp_path, _readers(ME))
    (status,) = evaluate_reviews(target, mounts, ME, kr, NOW)
    record_review_run(kr, TARGET, status, "rev1", {"pages": 4, "raised": 1}, NOW)
    (after,) = evaluate_reviews(target, mounts, ME, kr, NOW + timedelta(days=8))
    assert after.cursor == "rev1"
    (run,) = load_review_runs(kr, TARGET)
    assert run.counts == {"pages": 4, "raised": 1}
    assert run.at == "2026-11-20T12:00:00Z"
    with pytest.raises(ReviewRefused):
        record_review_run(kr, TARGET, after, "rev2", {"note": "some text"}, NOW)  # type: ignore[dict-item]


def test_run_against_a_changed_source_does_not_count(tmp_path: Path, kr: Path) -> None:
    mounts = _source(tmp_path, _readers(ME))
    t1 = _target(tmp_path, [_review()])
    (s1,) = evaluate_reviews(t1, mounts, ME, kr, NOW)
    record_review_run(kr, TARGET, s1, "rev1", {}, NOW)
    t2 = _target(tmp_path, [_review(source=OTHER_SRC)])
    (s2,) = evaluate_reviews(t2, mounts, ME, kr, NOW)
    assert s2.last_run is None and s2.cursor is None


def test_bad_interval_is_reported_and_never_runs(tmp_path: Path, kr: Path) -> None:
    target = _target(tmp_path, [_review(every="weekly")])
    (status,) = evaluate_reviews(target, _source(tmp_path, _readers(ME)), ME, kr, NOW)
    assert status.state == BAD_INTERVAL and not status.should_run


def test_parse_every() -> None:
    assert parse_every("7d") == timedelta(days=7)
    assert parse_every("12h") == timedelta(hours=12)
    assert parse_every("2w") == timedelta(weeks=2)
    assert parse_every("0d") is None
    assert parse_every("d") is None
    assert parse_every("1.5d") is None


def test_nothing_written_to_the_source(tmp_path: Path, kr: Path) -> None:
    target = _target(tmp_path, [_review()])
    mounts = _source(tmp_path, _readers(ME))
    before = sorted(p.relative_to(mounts[SRC]).as_posix() for p in mounts[SRC].rglob("*"))
    (status,) = evaluate_reviews(target, mounts, ME, kr, NOW)
    record_review_run(kr, TARGET, status, "rev1", {"pages": 1}, NOW)
    after = sorted(p.relative_to(mounts[SRC]).as_posix() for p in mounts[SRC].rglob("*"))
    assert before == after
    assert not str(state_path(kr, TARGET)).startswith(str(mounts[SRC]))


def test_directory_fallback_is_no_grant_and_cursor_not_advanced(tmp_path: Path, kr: Path) -> None:
    """A group the declaration never defines resolves reader-only in resolve_rights;
    a review needs an explicit grant, so it must not run."""
    target = _target(tmp_path, [_review()])
    mounts = _source(tmp_path, _readers("group:undefined-team"))
    (status,) = evaluate_reviews(target, mounts, ME, kr, NOW)
    assert status.state == NO_GRANT
    with pytest.raises(ReviewRefused):
        record_review_run(kr, TARGET, status, "rev1", {}, NOW)
    assert load_review_runs(kr, TARGET) == []


def test_unregistered_directory_is_no_grant(tmp_path: Path, kr: Path) -> None:
    target = _target(tmp_path, [_review()])
    root = tmp_path / "source"
    _declare(root, SRC, _readers("group:g"))
    path = root / "_store" / "access.yaml"
    data = yaml.safe_load(path.read_text())
    data["directory"] = "corp-ldap"
    path.write_text(yaml.safe_dump(data))
    (status,) = evaluate_reviews(target, {SRC: root}, ME, kr, NOW)
    assert status.state == NO_GRANT

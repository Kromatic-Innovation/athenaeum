# SPDX-License-Identifier: Apache-2.0
"""Tests for the test suite's environment contract (issue athenaeum#2136).

`tests/env_contract.py` is what turns an unmet machine precondition into a
NAMED skip instead of a bare failure, and -- under
`ATHENAEUM_TEST_ENV_CONTRACT=strict`, which CI's `clean-venv` job sets -- into a
failure that cannot be mistaken for a passing run. Both halves need proving:
a skip helper that silently never skips, and a strict mode that silently never
fails, are each worse than no helper at all.

`test_contributing_documents_every_property` is the anti-drift gate: the
contract is a promise to contributors, so every property this module enforces
must be named in CONTRIBUTING.md, where they will actually read it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests import env_contract
from tests.env_contract import PROPERTIES, require, strict, unmet

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_every_property_has_a_description_and_a_probe() -> None:
    assert PROPERTIES, "an empty contract registry enforces nothing"
    for name, (description, probe) in PROPERTIES.items():
        assert description.strip(), name
        assert callable(probe), name
        # A probe must answer, not raise, in whatever environment it runs in.
        result = probe()
        assert result is None or isinstance(result, str), name


def test_unmet_rejects_an_unknown_property() -> None:
    with pytest.raises(KeyError, match="unknown environment-contract property"):
        unmet("no_such_property")


def test_require_passes_through_when_the_property_is_met(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(PROPERTIES, "always_met", ("always met.", lambda: None))
    require("always_met")  # must not raise, must not skip


def test_require_skips_with_the_reason_when_unmet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        PROPERTIES, "never_met", ("never met.", lambda: "the widget is absent")
    )
    monkeypatch.delenv(env_contract.STRICT_ENV_VAR, raising=False)
    with pytest.raises(pytest.skip.Exception) as excinfo:
        require("never_met")
    assert "never_met" in str(excinfo.value)
    assert "the widget is absent" in str(excinfo.value)


def test_strict_mode_turns_the_skip_into_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The whole point of strict mode: in the `clean-venv` job a contract skip
    # would let a genuinely missing dependency hide behind "skipped".
    monkeypatch.setitem(
        PROPERTIES, "never_met", ("never met.", lambda: "the widget is absent")
    )
    monkeypatch.setenv(env_contract.STRICT_ENV_VAR, "strict")
    assert strict() is True
    with pytest.raises(pytest.fail.Exception) as excinfo:
        require("never_met")
    assert "the widget is absent" in str(excinfo.value)
    assert env_contract.STRICT_ENV_VAR in str(excinfo.value)


def test_strict_mode_does_not_fail_a_property_that_is_met(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(PROPERTIES, "always_met", ("always met.", lambda: None))
    monkeypatch.setenv(env_contract.STRICT_ENV_VAR, "strict")
    require("always_met")


@pytest.mark.parametrize("value", ["", "0", "lenient", "STRICTLY"])
def test_strict_is_only_the_exact_token(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(env_contract.STRICT_ENV_VAR, value)
    assert strict() is False


@pytest.mark.parametrize("value", ["strict", " strict ", "STRICT"])
def test_strict_is_case_and_whitespace_tolerant(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(env_contract.STRICT_ENV_VAR, value)
    assert strict() is True


def test_vector_backend_probe_agrees_with_importing_chromadb() -> None:
    try:
        import chromadb  # noqa: F401
    except ImportError:
        expected_unmet = True
    else:
        expected_unmet = False
    assert (unmet("vector_backend") is not None) is expected_unmet


def test_contributing_documents_every_property() -> None:
    # Anti-drift: the contract is a promise to contributors, so it has to be
    # written where they read it, not only where it is enforced.
    contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    for name in PROPERTIES:
        assert f"`{name}`" in contributing, (
            f"environment-contract property {name!r} is enforced by "
            "tests/env_contract.py but not documented in CONTRIBUTING.md"
        )

# SPDX-License-Identifier: Apache-2.0
"""Issue #519/#528 (M3): one malformed-env-value policy across the resolvers.

The audit found four incompatible behaviours for a mistyped numeric env var —
silent fall-through, silent return-default, a silent hard-zero, and a hard
crash — depending on which knob you typed it into. This pins the unified
contract: every swept numeric knob **WARNs (naming the variable) and falls
back**, so a malformed value behaves identically to an unset one. The small,
enumerated fail-loud exceptions (auto-apply threshold; the full-body token
cap's out-of-range guard) keep raising by design.
"""

from __future__ import annotations

import logging

import pytest

from athenaeum.config import (
    resolve_audit_sample_rate_t2_approvals,
    resolve_heartbeat_interval,
    resolve_lock_break_stale_after,
    resolve_lock_timeout,
    resolve_lock_warn_stale_after,
    resolve_min_merge_mean_similarity,
)
from athenaeum.resolutions import (
    resolve_auto_apply_threshold,
    resolve_full_body_token_cap,
    resolve_max_per_run,
)

# (label, resolver, env_var, config-with-a-yaml-value-so-fallback-is-observable)
_SWEPT_KNOBS = [
    (
        "min_merge_mean_similarity",
        resolve_min_merge_mean_similarity,
        "ATHENAEUM_MIN_MERGE_MEAN_SIMILARITY",
        {"librarian": {"min_merge_mean_similarity": 0.55}},
    ),
    (
        "lock_timeout",
        resolve_lock_timeout,
        "ATHENAEUM_LOCK_TIMEOUT",
        {"librarian": {"lock_timeout": 12.0}},
    ),
    (
        "heartbeat_interval",
        resolve_heartbeat_interval,
        "ATHENAEUM_HEARTBEAT_INTERVAL",
        {"librarian": {"heartbeat_interval": 45.0}},
    ),
    (
        "lock_break_stale_after",
        resolve_lock_break_stale_after,
        "ATHENAEUM_LOCK_BREAK_STALE_AFTER",
        {"librarian": {"lock_break_stale_after": 9000.0}},
    ),
    (
        "lock_warn_stale_after",
        resolve_lock_warn_stale_after,
        "ATHENAEUM_LOCK_WARN_STALE_AFTER",
        {"librarian": {"lock_warn_stale_after": 3000.0}},
    ),
    (
        "audit_sample_rate_t2_approvals",
        resolve_audit_sample_rate_t2_approvals,
        "ATHENAEUM_AUDIT_SAMPLE_RATE_T2_APPROVALS",
        {"librarian": {"audit_sample_rate_t2_approvals": 0.2}},
    ),
    (
        "resolve_max_per_run",
        resolve_max_per_run,
        "ATHENAEUM_RESOLVE_MAX_PER_RUN",
        {"contradiction": {"resolve_max_per_run": 7}},
    ),
    (
        "full_body_token_cap",
        resolve_full_body_token_cap,
        "ATHENAEUM_RESOLVE_FULL_BODY_TOKEN_CAP",
        {"resolve": {"full_body_token_cap": 2000}},
    ),
]


@pytest.mark.parametrize(
    "label,resolver,env_var,config",
    _SWEPT_KNOBS,
    ids=[k[0] for k in _SWEPT_KNOBS],
)
def test_malformed_env_warns_and_falls_back_identically_to_unset(
    label: str,
    resolver,
    env_var: str,
    config: dict,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Baseline: value with the env var unset.
    monkeypatch.delenv(env_var, raising=False)
    unset_value = resolver(config)

    # Malformed: must behave identically (fall back) AND log a WARNING that
    # names the offending variable — no silent fall-through / crash / hard-zero.
    monkeypatch.setenv(env_var, "definitely-not-a-number-42x")
    with caplog.at_level(logging.WARNING, logger="athenaeum.config"):
        malformed_value = resolver(config)

    assert malformed_value == unset_value, (
        f"{label}: malformed env changed the resolved value "
        f"({malformed_value!r} != unset {unset_value!r})"
    )
    assert any(
        env_var in r.message and "malformed" in r.message for r in caplog.records
    ), f"{label}: expected a WARNING naming {env_var}; got {caplog.text!r}"


class TestFailLoudExceptionsStillRaise:
    """The enumerated exceptions to the WARN-and-fall-back policy keep raising."""

    def test_auto_apply_threshold_malformed_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_RESOLVE_AUTO_APPLY_THRESHOLD", "0.9x")
        with pytest.raises(ValueError, match="ATHENAEUM_RESOLVE_AUTO_APPLY_THRESHOLD"):
            resolve_auto_apply_threshold(None)

    def test_auto_apply_threshold_out_of_range_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_RESOLVE_AUTO_APPLY_THRESHOLD", "9.0")
        with pytest.raises(ValueError, match="out of range"):
            resolve_auto_apply_threshold(None)

    def test_full_body_token_cap_nonpositive_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Malformed falls back (covered above); an out-of-range <= 0 still raises.
        monkeypatch.setenv("ATHENAEUM_RESOLVE_FULL_BODY_TOKEN_CAP", "0")
        with pytest.raises(ValueError, match="positive integer"):
            resolve_full_body_token_cap(None)

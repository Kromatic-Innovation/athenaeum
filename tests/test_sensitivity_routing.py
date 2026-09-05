# SPDX-License-Identifier: Apache-2.0
"""Tests for the ``sensitivity.routing`` config resolver (athenaeum#1022).

Slice 1/4 of athenaeum#949's design note (`docs/design/sensitivity-value-routing.md`
§8): `resolve_sensitivity_routing` in `athenaeum.config`. This slice is
behaviour-free — nothing reads the resolver yet — so these tests cover only
the resolver's own contract: default-off, an explicit `enabled: true`, a
per-class `action: off` override, the env override (both directions), and
the two fail-loud config-error cases.
"""

from __future__ import annotations

import pytest

from athenaeum.config import (
    VALID_SENSITIVITY_ROUTING_ACTIONS,
    SensitivityRoutingConfigError,
    resolve_sensitivity_routing,
)


class TestResolveSensitivityRouting:
    def test_default_is_off_with_no_classes(self) -> None:
        assert resolve_sensitivity_routing(None) == {"enabled": False, "classes": {}}
        assert resolve_sensitivity_routing({}) == {"enabled": False, "classes": {}}
        assert resolve_sensitivity_routing({"sensitivity": {}}) == {
            "enabled": False,
            "classes": {},
        }
        assert resolve_sensitivity_routing({"sensitivity": {"routing": {}}}) == {
            "enabled": False,
            "classes": {},
        }

    def test_yaml_enables_routing(self) -> None:
        config = {"sensitivity": {"routing": {"enabled": True}}}
        assert resolve_sensitivity_routing(config) == {"enabled": True, "classes": {}}

    def test_class_block_defaults_action_to_route(self) -> None:
        config = {
            "sensitivity": {
                "routing": {"enabled": True, "classes": {"pii": {}}},
            }
        }
        result = resolve_sensitivity_routing(config)
        assert result == {"enabled": True, "classes": {"pii": {"action": "route"}}}

    def test_per_class_action_off_override(self) -> None:
        config = {
            "sensitivity": {
                "routing": {
                    "enabled": True,
                    "classes": {"pii": {"action": "off"}},
                },
            }
        }
        result = resolve_sensitivity_routing(config)
        assert result == {"enabled": True, "classes": {"pii": {"action": "off"}}}

    def test_explicit_action_route_is_honored(self) -> None:
        config = {
            "sensitivity": {
                "routing": {
                    "enabled": True,
                    "classes": {"pii": {"action": "route"}},
                },
            }
        }
        result = resolve_sensitivity_routing(config)
        assert result == {"enabled": True, "classes": {"pii": {"action": "route"}}}

    def test_env_override_true_direction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_SENSITIVITY_ROUTING_ENABLED", "true")
        assert resolve_sensitivity_routing(None)["enabled"] is True
        # Case-insensitive.
        monkeypatch.setenv("ATHENAEUM_SENSITIVITY_ROUTING_ENABLED", "TRUE")
        assert resolve_sensitivity_routing(None)["enabled"] is True

    def test_env_override_false_direction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_SENSITIVITY_ROUTING_ENABLED", "false")
        config = {"sensitivity": {"routing": {"enabled": True}}}
        assert resolve_sensitivity_routing(config)["enabled"] is False
        # Case-insensitive.
        monkeypatch.setenv("ATHENAEUM_SENSITIVITY_ROUTING_ENABLED", "False")
        assert resolve_sensitivity_routing(config)["enabled"] is False

    def test_malformed_enabled_yaml_value_is_config_error(self) -> None:
        config = {"sensitivity": {"routing": {"enabled": "yes"}}}
        with pytest.raises(SensitivityRoutingConfigError):
            resolve_sensitivity_routing(config)

    def test_malformed_enabled_env_value_is_config_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_SENSITIVITY_ROUTING_ENABLED", "nope")
        with pytest.raises(SensitivityRoutingConfigError):
            resolve_sensitivity_routing(None)

    def test_unknown_class_action_is_config_error(self) -> None:
        config = {
            "sensitivity": {
                "routing": {
                    "enabled": True,
                    "classes": {"pii": {"action": "drop"}},
                },
            }
        }
        with pytest.raises(SensitivityRoutingConfigError):
            resolve_sensitivity_routing(config)

    def test_valid_actions_tuple(self) -> None:
        assert VALID_SENSITIVITY_ROUTING_ACTIONS == ("route", "off")

    def test_blank_and_non_string_class_names_are_dropped(self) -> None:
        config = {
            "sensitivity": {
                "routing": {
                    "enabled": True,
                    "classes": {"": {"action": "route"}, "  ": {"action": "route"}},
                },
            }
        }
        assert resolve_sensitivity_routing(config) == {"enabled": True, "classes": {}}

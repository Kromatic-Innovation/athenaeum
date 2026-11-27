# SPDX-License-Identifier: Apache-2.0
"""The per-vault read check of one tool call on a served process (issue athenaeum#3176).

Layering: L4 (imports :mod:`athenaeum.access`, L3, and :mod:`athenaeum.audit_log`,
L1).

Design: ``docs/design/security-posture.md`` ("A read grant on the vault for a
sensitive read").

A :class:`VaultReadGate` is built once per tool call, for the principal of the
request being served, and handed to the places a vault's values are read as a
``vault_read_allowed`` callable. It asks :func:`athenaeum.access.can` at most
once per vault per call (never per hit), so :mod:`athenaeum.mcp_server` and the
search path import no access code themselves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from athenaeum.access import can
from athenaeum.audit_log import record_sensitive_read

__all__ = ["VaultReadGate"]


class VaultReadGate:
    """Callable with a vault (storage location) name; true when the principal may read it.

    No principal holds nothing, and a location the configuration cannot name is
    refused. Each vault is asked once per call and each answer is written to
    the server's audit log (principal, vault, outcome; never a value) BEFORE it
    is returned, so no read follows an unrecorded decision: an unwritable log
    raises and the call fails closed. ``denied`` says whether any vault refused.
    """

    def __init__(
        self,
        principal: tuple[str, ...] | None,
        *,
        tool_name: str,
        config: dict[str, Any] | None,
        knowledge_root: Path,
        audit_log_path: Path | None,
    ) -> None:
        self._principal = principal
        self._tool_name = tool_name
        self._config = config
        self._knowledge_root = knowledge_root
        self._audit_log_path = audit_log_path
        self._answers: dict[str, bool] = {}
        self.denied = False

    def __call__(self, vault: str) -> bool:
        if vault in self._answers:
            return self._answers[vault]
        try:
            allowed = can(
                self._principal,
                vault,
                "read",
                config=self._config,
                knowledge_root=self._knowledge_root,
            )
        except ValueError:
            allowed = False
        if self._audit_log_path is not None:
            record_sensitive_read(
                self._audit_log_path,
                principal=self._principal,
                vault=vault,
                outcome="granted" if allowed else "denied",
                tool_name=self._tool_name,
            )
        self._answers[vault] = allowed
        if not allowed:
            self.denied = True
        return allowed

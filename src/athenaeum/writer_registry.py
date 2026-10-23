# SPDX-License-Identifier: Apache-2.0
"""Writer registry: names for agents and adapters that write facts (issue athenaeum#2041).

A writer-registry entry labels a writer; it does not verify one, and it never confers authority.

The registry gives a human-readable label to a writer identity key, so a
report or a reviewer can say "release-bot" instead of a bare ``(iss, sub)``
tuple. That is all it does. Nothing in :mod:`athenaeum.supersession`,
:mod:`athenaeum.asserter_authority`, :mod:`athenaeum.answers` or
:mod:`athenaeum.tiers` consults it, and none may: a name that anyone with
filesystem access can add to a YAML file is exactly as self-declared as the
frontmatter ``asserter`` block it labels. Verification (a signed or
operator-held key) is deliberately out of scope and is tracked in
athenaeum#2039.

Reuse, not reinvention:

- A writer's kind is one of :data:`athenaeum.models.ASSERTER_TYPES`
  (``person`` / ``software_agent`` / ``organization``).
- A writer's identity key is :func:`athenaeum.models.asserter_identity_key`
  over the entry's ``iss`` / ``sub`` (or Entra ``provider_ids``). This module
  derives no key of its own, so the OIDC-durable and Entra-pairwise rules
  live in exactly one place.

Format + location (mirrors :mod:`athenaeum.authority`'s manifest loader):

.. code-block:: yaml

    version: 1
    writers:
      - label: release-bot
        type: software_agent
        iss: https://idp.example.org
        sub: svc-release-bot

``version`` must be the literal integer ``1``. The path resolves through
:func:`athenaeum.config.resolve_writer_registry_path` (env
``ATHENAEUM_WRITER_REGISTRY`` > yaml ``librarian.writer_registry_path`` >
``<knowledge_root>/writer-registry.yaml``). A missing file is an EMPTY
registry; a file that exists but is malformed raises
:class:`WriterRegistryError` naming the defect.

Layering: L1 (reads :mod:`athenaeum.models` only). No config-layer or LLM
imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from athenaeum.models import ASSERTER_TYPES, asserter_identity_key

__all__ = [
    "LABELS_NOT_VERIFIES_SENTENCE",
    "SUPPORTED_REGISTRY_VERSION",
    "WriterEntry",
    "WriterRegistry",
    "WriterRegistryError",
    "load_writer_registry",
    "parse_writer_registry",
]

#: The one schema version this loader understands.
SUPPORTED_REGISTRY_VERSION = 1

#: Stated verbatim in this module's docstring and in
#: ``docs/design/writer-identity.md``; a test pins both.
LABELS_NOT_VERIFIES_SENTENCE = (
    "A writer-registry entry labels a writer; it does not verify one, "
    "and it never confers authority."
)


class WriterRegistryError(ValueError):
    """Raised when the writer registry file is malformed.

    Loud by design, like :class:`athenaeum.authority.AuthorityManifestError`:
    a registry an operator started must never silently read as empty.
    """


@dataclass(frozen=True)
class WriterEntry:
    """One labelled writer. ``identity_key`` is never empty."""

    label: str
    kind: str
    identity_key: tuple[str, ...]


@dataclass(frozen=True)
class WriterRegistry:
    """A loaded, validated registry. Labels only; see the module docstring."""

    version: int
    writers: tuple[WriterEntry, ...] = ()

    def lookup(self, identity_key: tuple[str, ...]) -> WriterEntry | None:
        """Return the entry labelled for *identity_key*, or ``None``."""
        if not identity_key:
            return None
        for entry in self.writers:
            if entry.identity_key == tuple(identity_key):
                return entry
        return None

    def lookup_asserter(self, asserter: dict[str, object] | None) -> WriterEntry | None:
        """Label an ``asserter`` block by its :func:`asserter_identity_key`."""
        return self.lookup(asserter_identity_key(asserter))


def _require_nonempty_str(value: Any, field_name: str, *, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WriterRegistryError(
            f"writer registry: {where} has a missing/empty {field_name!r} field"
        )
    return value.strip()


def parse_writer_registry(text: str) -> WriterRegistry:
    """Parse + validate registry YAML text.

    Raises :class:`WriterRegistryError` naming the defect: unparseable YAML,
    an empty or non-mapping document, a missing or wrong ``version``, a
    non-list ``writers``, an entry that is not a mapping, a missing
    ``label``, a ``type`` outside :data:`~athenaeum.models.ASSERTER_TYPES`,
    an entry from which :func:`~athenaeum.models.asserter_identity_key`
    derives no key, a duplicate label, or a duplicate identity key.
    """
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise WriterRegistryError(f"writer registry: invalid YAML ({exc})") from exc

    if raw is None:
        raise WriterRegistryError("writer registry: empty document")
    if not isinstance(raw, dict):
        raise WriterRegistryError(
            "writer registry: top-level document must be a mapping "
            f"(got {type(raw).__name__})"
        )

    version = raw.get("version")
    if version != SUPPORTED_REGISTRY_VERSION:
        raise WriterRegistryError(
            f"writer registry: unsupported version {version!r} "
            f"(expected {SUPPORTED_REGISTRY_VERSION})"
        )

    raw_writers = raw.get("writers")
    if raw_writers is None:
        raw_writers = []
    if not isinstance(raw_writers, list):
        raise WriterRegistryError("writer registry: 'writers' must be a list")

    writers: list[WriterEntry] = []
    seen_labels: set[str] = set()
    seen_keys: set[tuple[str, ...]] = set()
    for idx, entry in enumerate(raw_writers):
        where = f"writers[{idx}]"
        if not isinstance(entry, dict):
            raise WriterRegistryError(
                f"writer registry: {where} must be a mapping (got {type(entry).__name__})"
            )
        label = _require_nonempty_str(entry.get("label"), "label", where=where)
        if label in seen_labels:
            raise WriterRegistryError(f"writer registry: duplicate writer label {label!r}")
        seen_labels.add(label)

        kind = _require_nonempty_str(entry.get("type"), "type", where=where)
        if kind not in ASSERTER_TYPES:
            raise WriterRegistryError(
                f"writer registry: {where} ({label!r}) type {kind!r} is not one of "
                f"{sorted(ASSERTER_TYPES)}"
            )

        identity_fields: dict[str, object] = {
            k: entry[k] for k in ("iss", "sub", "provider_ids") if k in entry
        }
        key = asserter_identity_key(identity_fields)
        if not key:
            raise WriterRegistryError(
                f"writer registry: {where} ({label!r}) carries no derivable identity "
                "key (needs 'iss' with 'sub', or 'iss' with 'provider_ids' "
                "entra_tid and entra_oid)"
            )
        if key in seen_keys:
            raise WriterRegistryError(
                f"writer registry: duplicate identity key {key!r} ({label!r})"
            )
        seen_keys.add(key)
        writers.append(WriterEntry(label=label, kind=kind, identity_key=key))

    return WriterRegistry(version=SUPPORTED_REGISTRY_VERSION, writers=tuple(writers))


def load_writer_registry(path: Path) -> WriterRegistry:
    """Load + validate the registry at *path*.

    A missing file returns an EMPTY registry (not an error): an
    unconfigured knowledge base labels no writers. A file that exists but is
    malformed raises :class:`WriterRegistryError`.
    """
    if not path.is_file():
        return WriterRegistry(version=SUPPORTED_REGISTRY_VERSION)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WriterRegistryError(
            f"writer registry: could not read {path} ({exc})"
        ) from exc
    return parse_writer_registry(text)

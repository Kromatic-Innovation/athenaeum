# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#2333 (TG5): ``athenaeum.config.resolve_witness_keys``.

Excluded from ``tests/test_config_resolver_parity_generic.py``'s generic
probe (``_STRUCTURED_CONTAINER_VALUE`` -- see that file's comment) because
the resolver's leaf value is a dict keyed by operator-chosen key ids whose
OWN values are structured objects with named fields (``public_key`` /
``strength``) no generic sentinel spells out. This file is the direct
"yaml key is actually read" proof that exclusion defers to, plus the
defensive-parsing behaviour the issue's "yaml only, defensive parsing,
default empty" line asks for.

A base64-of-raw-bytes public key, never a PEM envelope: this is a
public-KEY feature, and these fixtures build keys via ``cryptography``'s
own ``public_bytes(Encoding.Raw, PublicFormat.Raw)`` so no
``-----BEGIN``/``KEY----`` banner pair ever appears in this file (the
stock gitleaks ``private-key`` rule's trigger shape) -- no private key
material is written anywhere, committed or not.
"""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from athenaeum.config import WITNESS_KEY_STRENGTHS, resolve_witness_keys
from promptwitness.signing import STRENGTHS as PROMPTWITNESS_STRENGTHS


def _raw_public_key_b64() -> str:
    """A fresh Ed25519 public key, raw 32 bytes, standard base64 -- never
    a PEM-serialized private key, per the module docstring."""
    private_key = Ed25519PrivateKey.generate()
    raw = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def test_strengths_match_promptwitness_signing() -> None:
    """Parity pin (mirrors ``promptwitness.signing.derive_key_id``'s own
    "duplicate + parity test" discipline against
    ``athenaeum.receipt_sealers.derive_key_id``): the duplicated tuple in
    ``athenaeum.config`` must never drift from the real vocabulary."""
    assert WITNESS_KEY_STRENGTHS == PROMPTWITNESS_STRENGTHS


class TestYamlKeyIsRead:
    def test_default_is_empty(self) -> None:
        assert resolve_witness_keys(None) == {}
        assert resolve_witness_keys({}) == {}
        assert resolve_witness_keys({"librarian": {}}) == {}

    def test_reads_a_well_formed_entry(self) -> None:
        pub_b64 = _raw_public_key_b64()
        config = {
            "librarian": {
                "witness": {
                    "keys": {
                        "ed25519:abcdef0123456789": {
                            "public_key": pub_b64,
                            "strength": "os-keystore",
                        }
                    }
                }
            }
        }
        resolved = resolve_witness_keys(config)
        assert set(resolved) == {"ed25519:abcdef0123456789"}
        entry = resolved["ed25519:abcdef0123456789"]
        assert entry["public_key"] == base64.b64decode(pub_b64)
        assert len(entry["public_key"]) == 32
        assert entry["strength"] == "os-keystore"

    def test_reads_multiple_entries(self) -> None:
        pub1, pub2 = _raw_public_key_b64(), _raw_public_key_b64()
        config = {
            "librarian": {
                "witness": {
                    "keys": {
                        "k1": {"public_key": pub1, "strength": "software-file"},
                        "k2": {"public_key": pub2, "strength": "hardware-presence"},
                    }
                }
            }
        }
        resolved = resolve_witness_keys(config)
        assert set(resolved) == {"k1", "k2"}
        assert resolved["k1"]["strength"] == "software-file"
        assert resolved["k2"]["strength"] == "hardware-presence"


class TestDefensiveParsing:
    """Issue athenaeum#2333: "defensive parsing, default empty" -- one
    malformed entry drops only itself, never the whole map, and nothing
    here ever raises."""

    def test_non_dict_config_is_empty(self) -> None:
        assert resolve_witness_keys("not-a-dict") == {}  # type: ignore[arg-type]

    def test_librarian_not_a_dict(self) -> None:
        assert resolve_witness_keys({"librarian": "nope"}) == {}

    def test_witness_not_a_dict(self) -> None:
        assert resolve_witness_keys({"librarian": {"witness": "nope"}}) == {}

    def test_keys_not_a_dict(self) -> None:
        assert resolve_witness_keys({"librarian": {"witness": {"keys": ["nope"]}}}) == {}

    def test_entry_not_a_dict_is_dropped(self) -> None:
        config = {"librarian": {"witness": {"keys": {"k1": "not-a-mapping"}}}}
        assert resolve_witness_keys(config) == {}

    def test_entry_missing_public_key_is_dropped(self) -> None:
        config = {"librarian": {"witness": {"keys": {"k1": {"strength": "os-keystore"}}}}}
        assert resolve_witness_keys(config) == {}

    def test_entry_missing_strength_is_dropped(self) -> None:
        config = {
            "librarian": {
                "witness": {"keys": {"k1": {"public_key": _raw_public_key_b64()}}}
            }
        }
        assert resolve_witness_keys(config) == {}

    def test_entry_with_unknown_strength_is_dropped(self) -> None:
        config = {
            "librarian": {
                "witness": {
                    "keys": {
                        "k1": {"public_key": _raw_public_key_b64(), "strength": "quantum"}
                    }
                }
            }
        }
        assert resolve_witness_keys(config) == {}

    def test_entry_with_non_base64_public_key_is_dropped(self) -> None:
        config = {
            "librarian": {
                "witness": {
                    "keys": {
                        "k1": {"public_key": "not!base64!!", "strength": "os-keystore"}
                    }
                }
            }
        }
        assert resolve_witness_keys(config) == {}

    def test_entry_with_wrong_length_key_is_dropped(self) -> None:
        short = base64.b64encode(b"short").decode("ascii")
        config = {
            "librarian": {
                "witness": {"keys": {"k1": {"public_key": short, "strength": "os-keystore"}}}
            }
        }
        assert resolve_witness_keys(config) == {}

    def test_one_malformed_entry_does_not_drop_its_siblings(self) -> None:
        good = _raw_public_key_b64()
        config = {
            "librarian": {
                "witness": {
                    "keys": {
                        "good": {"public_key": good, "strength": "os-keystore"},
                        "bad": {"public_key": "not!base64!!", "strength": "os-keystore"},
                        "also-bad": {"strength": "os-keystore"},
                    }
                }
            }
        }
        resolved = resolve_witness_keys(config)
        assert set(resolved) == {"good"}

    def test_empty_key_id_is_dropped(self) -> None:
        config = {
            "librarian": {
                "witness": {
                    "keys": {"": {"public_key": _raw_public_key_b64(), "strength": "none"}}
                }
            }
        }
        assert resolve_witness_keys(config) == {}

    def test_non_string_key_id_is_dropped(self) -> None:
        config = {
            "librarian": {
                "witness": {
                    "keys": {1: {"public_key": _raw_public_key_b64(), "strength": "none"}}
                }
            }
        }
        assert resolve_witness_keys(config) == {}

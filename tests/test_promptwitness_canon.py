# SPDX-License-Identifier: Apache-2.0
"""Tests for promptwitness.canon (athenaeum#2147).

The normalizer parity tests bind ``promptwitness.canon.normalize`` to
``athenaeum.transcript_verify._normalize`` by actually CALLING both
functions over a shared fixture corpus and asserting equality -- never by
turning one into a re-export of the other (the issue is explicit that the
athenaeum leaf must not depend on a package athenaeum#2162 may extract).
"""

from __future__ import annotations

import hashlib

import pytest

from athenaeum.transcript_verify import _normalize as athenaeum_normalize
from promptwitness import canon

#: Non-ASCII, tabs, CRLF, and empty input, as the issue's AC names explicitly,
#: plus a few adjacent edge shapes (mixed whitespace, emoji, all-whitespace).
_CORPUS: tuple[str, ...] = (
    "",
    "   ",
    "Hello World",
    "hello\tworld",
    "hello\r\nworld",
    "  leading and trailing  ",
    "MIXED Case TEXT",
    "café éèê naïve",  # accented non-ASCII
    "日本語のテキスト",  # CJK
    "emoji \U0001f600 test",
    "tabs\t\t\tand\n\n\nnewlines",
    "a non-breaking space",  # NBSP is not split by str.split()
    "single",
)


class TestNormalizeParity:
    @pytest.mark.parametrize("text", _CORPUS)
    def test_matches_transcript_verify_normalize(self, text: str) -> None:
        assert canon.normalize(text) == athenaeum_normalize(text)

    def test_parity_is_not_vacuous(self) -> None:
        """Prove the two functions could actually disagree if one were
        wrong -- a corpus of only no-op inputs would pass trivially."""
        assert canon.normalize("  Hello\tWORLD  ") == "hello world"
        assert athenaeum_normalize("  Hello\tWORLD  ") == "hello world"

    def test_source_shape_matches(self) -> None:
        """Belt-and-suspenders: the two functions are pinned to the exact
        same one-line implementation, not just the same observed output
        over this corpus."""
        import inspect

        pw_src = inspect.getsource(canon.normalize).strip().splitlines()[-1].strip()
        ath_src = inspect.getsource(athenaeum_normalize).strip().splitlines()[-1].strip()
        assert pw_src == ath_src == 'return " ".join(text.split()).lower()'


class TestDigest:
    def test_shape(self) -> None:
        d = canon.digest("hello")
        assert d.startswith("sha256:")
        assert len(d) == len("sha256:") + 64

    def test_matches_raw_sha256(self) -> None:
        text = "hello world"
        assert canon.digest(text) == "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()

    def test_different_text_different_digest(self) -> None:
        assert canon.digest("a") != canon.digest("b")

    def test_empty_string(self) -> None:
        assert canon.digest("") == "sha256:" + hashlib.sha256(b"").hexdigest()


class TestUtteranceDigestRecomputeParity:
    """AC: 'existing receipt utterance_digest values in a fixture ledger
    recompute byte-identically.'

    ``athenaeum.transcript_receipts``'s own ``utterance_digest`` is
    ``sha256(_normalize(matched_text)).hexdigest()`` with no "sha256:"
    prefix (see ``_matching_human_utterance`` returning the already-
    normalized text, then ``hashlib.sha256(matched.encode()).hexdigest()``
    at the call site). There is no historical ledger with real values to
    pull from, so this fixture recomputes the SAME relationship from the
    real athenaeum code path and asserts promptwitness reproduces it
    byte-for-byte -- the reversible default recorded in the lane report.
    """

    @pytest.mark.parametrize(
        "raw_utterance",
        (
            "I think we should use Postgres for this.",
            "Café\tnaïve\r\ntext with  weird   whitespace",
            "",
        ),
    )
    def test_promptwitness_digest_matches_receipt_utterance_digest(
        self, raw_utterance: str
    ) -> None:
        # The real athenaeum call path (transcript_receipts.py): normalize,
        # then sha256-hexdigest with no prefix.
        normalized = athenaeum_normalize(raw_utterance)
        utterance_digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()

        # promptwitness's own path: normalize, then canon.digest (which adds
        # the "sha256:" prefix) over the normalized text.
        pw_digest = canon.digest(canon.normalize(raw_utterance))

        assert pw_digest == "sha256:" + utterance_digest


class TestCanonicalJson:
    def test_sorts_keys(self) -> None:
        assert canon.canonical_json({"b": 1, "a": 2}) == b'{"a":2,"b":1}'

    def test_compact_separators(self) -> None:
        assert canon.canonical_json({"a": [1, 2], "b": "x"}) == b'{"a":[1,2],"b":"x"}'

    def test_ensure_ascii_false_keeps_utf8_bytes(self) -> None:
        out = canon.canonical_json({"a": "café"})
        assert out == '{"a":"café"}'.encode("utf-8")
        assert b"\\u" not in out

    def test_deterministic_regardless_of_input_dict_order(self) -> None:
        a = {"z": 1, "a": 2, "m": 3}
        b = {"a": 2, "m": 3, "z": 1}
        assert canon.canonical_json(a) == canon.canonical_json(b)

    def test_nested_structures(self) -> None:
        obj = {"seq": 1, "segments": [{"kind": "typed", "start": 0, "end": 3}]}
        out = canon.canonical_json(obj)
        assert out == json_round_trip_bytes(obj)


def json_round_trip_bytes(obj: object) -> bytes:
    import json

    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )

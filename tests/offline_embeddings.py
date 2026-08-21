# SPDX-License-Identifier: Apache-2.0
"""Deterministic, offline, lexical stand-in for chromadb's default embedding
function (issue athenaeum#1091).

``VectorBackend``'s default model (``DEFAULT_EMBEDDING_MODEL``) is handled by
passing ``embedding_function=None`` through to chromadb, which resolves that
to its own built-in ``DefaultEmbeddingFunction`` -> ``ONNXMiniLM_L6_V2``. That
class downloads its ONNX model over HTTP the first time it is actually
*called* (``chromadb/utils/embedding_functions/onnx_mini_lm_l6_v2.py``,
``httpx.stream("GET", url)``) — regardless of whether the caller ever passed
chromadb an explicit embedding function, because chromadb's own
``get_collection``/``_embed`` fall-through resolves an unspecified EF back to
``DefaultEmbeddingFunction`` (see ``tests/conftest.py`` for the exact call
chain and why patching ``ONNXMiniLM_L6_V2`` itself — not
``VectorBackend._embedding_function`` — is the seam that actually intercepts
every default-model code path, including the read-side ``query()``/``add()``
calls that never pass an embedding function explicitly at all).

This module supplies the deterministic replacement: tokenize on
non-alphanumerics, lowercase, hash each token into a fixed 384-dim slot with
a stable (non-``hash()``) hash, L2-normalize. It intentionally mirrors the
shape of ``athenaeum.clusters._fallback_embeddings`` (which faces the same
"chromadb unavailable" problem in production) but fixes that helper's use of
the PYTHONHASHSEED-randomized builtin ``hash()`` — a stable hash is required
here since two different pytest-xdist workers (or two runs of the same
process) must produce byte-identical vectors for the delta/equivalence tests
to be meaningful.

Test-only. Never imported from ``src/athenaeum``.
"""

from __future__ import annotations

import math
import random
import re
import zlib
from collections.abc import Sequence

DIM = 384

# Magnitude of the per-document tie-breaking salt applied in ``embed_one``
# below — four orders of magnitude below a single token's unit contribution,
# so it cannot alter genuine lexical-overlap ranking, only break EXACT ties
# between documents with disjoint token sets.
_SALT_EPSILON = 1e-4

_TOKEN_RE = re.compile(r"[^0-9a-z]+")


def tokenize(text: str) -> list[str]:
    """Lowercase and split on runs of non-alphanumeric characters.

    ``Build-measure-learn`` -> ``["build", "measure", "learn"]``.
    """
    return [tok for tok in _TOKEN_RE.split(text.lower()) if tok]


def _stable_hash(token: str) -> int:
    """A hash that is stable across processes/runs (unlike builtin ``hash()``,
    which is randomized per-process by ``PYTHONHASHSEED`` for ``str``).

    ``zlib.crc32`` rather than ``hashlib.sha256`` deliberately:
    ``tests/test_search.py``'s ``hash_spy`` fixture monkeypatches
    ``athenaeum.search.hashlib.sha256`` to count real file-body hash calls
    (``TestFullReHashBackstopVector`` etc.) — since ``hashlib`` is a shared
    module singleton, this embedder calling ``hashlib.sha256`` too would
    silently inflate that count (observed: 39 calls instead of the expected
    3, once per token instead of once per file). ``zlib.crc32`` sidesteps
    the collision entirely, and cryptographic strength is irrelevant here.
    """
    return zlib.crc32(token.encode("utf-8"))


def _apply_tie_break_salt(vec: list[float], text: str, *, dim: int) -> None:
    """Add a tiny, deterministic, full-spectrum perturbation seeded by the
    whole document text (issue athenaeum#1091).

    Pure per-token hashing lets two documents with NO shared tokens land at
    an EXACTLY zero dot product against a given query (disjoint nonzero
    dims). chromadb's degenerate-result-set guard
    (``DegradedIndexError``, athenaeum#489 AC3) then fires on what should be
    an ordinary "neither document is relevant" case, not a corrupted index
    — observed on ``TestVectorIncremental.test_delete_page``, where the two
    surviving documents both scored an identical distance from a query
    naming the deleted (fintech) document's only distinctive tokens. Real
    embeddings essentially never produce an exact numeric tie between two
    different inputs; this salt restores that property for the test double,
    at a magnitude (``_SALT_EPSILON``) far below any genuine lexical-overlap
    signal, so it cannot flip a real ranking — only break an exact tie.
    """
    seed = zlib.crc32(text.encode("utf-8"))
    rng = random.Random(seed)
    for i in range(dim):
        vec[i] += _SALT_EPSILON * (rng.random() * 2.0 - 1.0)


def embed_one(text: str, *, dim: int = DIM) -> list[float]:
    """Hashing-trick bag-of-words embedding, unit-normalized (L2)."""
    vec = [0.0] * dim
    tokens = tokenize(text)
    if not tokens:
        # Degenerate (empty) input still needs a well-defined unit vector.
        vec[0] = 1.0
        return vec
    for tok in tokens:
        idx = _stable_hash(tok) % dim
        sign = 1.0 if (_stable_hash(tok + "\x00sign") % 2 == 0) else -1.0
        vec[idx] += sign
    _apply_tie_break_salt(vec, text, dim=dim)
    norm = math.sqrt(sum(x * x for x in vec))
    if norm > 0.0:
        vec = [x / norm for x in vec]
    return vec


def embed_many(texts: Sequence[str], *, dim: int = DIM) -> list[list[float]]:
    return [embed_one(t, dim=dim) for t in texts]


class OfflineONNXMiniLMStub:
    """Drop-in replacement for chromadb's ``ONNXMiniLM_L6_V2``.

    Constructed with no required args (matches
    ``ONNXMiniLM_L6_V2(preferred_providers=None)``'s call shape used by
    ``DefaultEmbeddingFunction.__call__``) and callable with a positional
    ``input`` list of strings, returning a same-shape list of 384-dim
    vectors. Never touches the network.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    def __call__(self, input: Sequence[str]) -> list[list[float]]:
        return embed_many(list(input))

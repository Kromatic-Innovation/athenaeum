# SPDX-License-Identifier: Apache-2.0
"""RFC 3161 anchoring adapter for promptwitness: an OPTIONAL second,
independent attestation source (athenaeum#2157), alongside the mandatory
``LocalClockAnchorer`` and the primary ``OpenTimestampsAnchorer``
(athenaeum#2151, :mod:`promptwitness.anchor`).

Why a second source at all: OpenTimestamps confirmation takes hours (it
waits for a Bitcoin block); an RFC 3161 Time-Stamp Authority (TSA) token is
immediate and comes from an independent party. Neither replaces the
other -- each is recorded as its own ``anchor`` line with its own
``source``.

**Module layout note.** The issue body named this module
``src/promptwitness/anchor/rfc3161.py`` -- a package submodule.
athenaeum#2151 shipped ``anchor`` as a single MODULE
(``src/promptwitness/anchor.py``), not a package, merged hours before this
issue was picked up; converting it to a package here is out of scope and
would needlessly churn that file. This adapter is instead a SIBLING
module, ``anchor_rfc3161.py`` -- a recorded, reversible decision
(athenaeum#2157).

Layering: same discipline as ``anchor.py`` -- L1.5, standard library only
at import time, and no ``athenaeum`` import, ever
(``tests/test_promptwitness_boundary.py`` covers this file too). Because
``src/promptwitness/`` cannot read athenaeum's own flag resolver, the
operator-facing key ``librarian.receipt_anchor.rfc3161_url`` is wired by
the athenaeum/CLI layer, never read from here directly; this module only
knows an explicit override and two env vars
(``PROMPTWITNESS_RFC3161_URL``, ``PROMPTWITNESS_RFC3161_CA_BUNDLE``),
mirroring ``anchor.resolve_block_source``'s precedence exactly -- see
:func:`resolve_rfc3161_url` / :func:`resolve_rfc3161_ca_bundle`. Neither
hard-codes a default TSA URL: an unconfigured URL means the adapter is
not offered at all (``promptwitness.cli._cmd_anchor``).

Subprocess/network seams, mirroring ``OpenTimestampsAnchorer``'s
``_http_get`` discipline: ``run`` (defaults to a ``subprocess.run``-based
helper that shells out to ``openssl ts -query`` / ``openssl ts -verify``)
and ``http_post`` (defaults to a ``urllib.request`` POST with
``Content-Type: application/timestamp-query``), both injectable so tests
feed recorded bytes and never touch the network or a real ``openssl``
binary.

**Failure never raises.** A missing ``openssl`` binary, a non-zero
``openssl`` exit, a network error, or a timeout all degrade to
``Attestation(status="failed")`` / ``VerifyResult(status="failed")`` with
a ``detail["reason"]``, never an exception.
"""

from __future__ import annotations

import os
import subprocess
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from urllib.parse import urlparse

from promptwitness.anchor import Attestation, VerifyResult, _now_iso_ms, _short, subject_safe

__all__ = [
    "Rfc3161Anchorer",
    "resolve_rfc3161_url",
    "resolve_rfc3161_ca_bundle",
]

_RunFn = Callable[[Sequence[str], "bytes | None", float], "subprocess.CompletedProcess[bytes]"]
_HttpPostFn = Callable[[str, bytes, float], bytes]


def resolve_rfc3161_url(override: str | None = None) -> str | None:
    """Precedence: *override* (``--rfc3161-url``) > ``PROMPTWITNESS_RFC3161_URL``
    env > ``None``. There is no public default: unlike
    :func:`promptwitness.anchor.resolve_block_source`, no TSA is free to
    nominate on an operator's behalf (choosing/paying for one is out of
    scope -- athenaeum#2157), so ``None`` means "not configured", which the
    CLI reads as "do not offer this adapter at all".
    """
    if override:
        return override
    env = os.environ.get("PROMPTWITNESS_RFC3161_URL")
    if env:
        return env
    return None


def resolve_rfc3161_ca_bundle(override: str | None = None) -> str | None:
    """Precedence: *override* (``--rfc3161-ca-bundle``) > ``PROMPTWITNESS_RFC3161_CA_BUNDLE``
    env > ``None``. A verify pass with no bundle configured fails cleanly
    (``verify_source="rfc3161-ca:unconfigured"``) rather than guessing a
    system trust store.
    """
    if override:
        return override
    env = os.environ.get("PROMPTWITNESS_RFC3161_CA_BUNDLE")
    if env:
        return env
    return None


def _default_run(
    args: Sequence[str], data: bytes | None, timeout: float
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(list(args), input=data, capture_output=True, timeout=timeout, check=False)


def _default_http_post(url: str, data: bytes, timeout: float) -> bytes:
    req = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/timestamp-query"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _short_bytes(data: bytes) -> str:
    """Same length budget as :func:`promptwitness.anchor._short`, for raw
    ``openssl`` stdout/stderr bytes rather than an exception."""
    return data.decode("utf-8", errors="replace").strip()[:200]


class Rfc3161Anchorer:
    """RFC 3161 TSA adapter. ``source`` is the instance attribute
    ``f"rfc3161:{<host of tsa_url>}"`` -- the ``Anchorer`` protocol declares
    ``source: str``, which an instance attribute satisfies just as well as
    a class attribute (compare ``LocalClockAnchorer``/``OpenTimestampsAnchorer``,
    which use a class attribute because their source never varies by
    instance).

    Proofs are stored at ``<ledger_dir>/anchors/<subject-safe>.tsr`` -- the
    raw TSA reply only. No request file is persisted: the query is built
    with ``-no_nonce`` precisely so ``verify()`` never needs one back --
    ``openssl ts -verify`` accepts ``-digest`` directly as an alternative
    to ``-queryfile``.
    """

    def __init__(
        self,
        ledger_dir: str | os.PathLike[str],
        *,
        tsa_url: str,
        ca_bundle: str | None = None,
        openssl: str = "openssl",
        run: _RunFn | None = None,
        http_post: _HttpPostFn | None = None,
    ) -> None:
        self._ledger_dir = Path(ledger_dir)
        self._tsa_url = tsa_url
        self._ca_bundle = ca_bundle
        self._openssl = openssl
        self._run: _RunFn = run or _default_run
        #: HTTP seam for the TSA POST -- tests inject a fake to feed
        #: recorded reply bytes without any network access.
        self._http_post: _HttpPostFn = http_post or _default_http_post

        parsed = urlparse(tsa_url)
        host = parsed.hostname or parsed.netloc or tsa_url
        self.source = f"rfc3161:{host}"

    def _proof_path(self, subject: str) -> Path:
        return self._ledger_dir / "anchors" / f"{subject_safe(subject)}.tsr"

    # -- anchor --------------------------------------------------------

    def anchor(self, digest: bytes, subject: str, *, timeout: float = 10.0) -> Attestation:
        query_args = [
            self._openssl,
            "ts",
            "-query",
            "-digest",
            digest.hex(),
            "-sha256",
            "-cert",
            "-no_nonce",
        ]
        try:
            query_result = self._run(query_args, None, timeout)
        except FileNotFoundError as exc:
            return Attestation(
                source=self.source,
                status="failed",
                claimed_time=_now_iso_ms(),
                proof_path=None,
                detail={"reason": "openssl-missing", "error": _short(exc)},
            )
        except Exception as exc:  # noqa: BLE001 - subprocess timeout/OS error: never raise
            return Attestation(
                source=self.source,
                status="failed",
                claimed_time=_now_iso_ms(),
                proof_path=None,
                detail={"reason": "query-failed", "error": _short(exc)},
            )
        if query_result.returncode != 0 or not query_result.stdout:
            return Attestation(
                source=self.source,
                status="failed",
                claimed_time=_now_iso_ms(),
                proof_path=None,
                detail={
                    "reason": "query-failed",
                    "error": _short_bytes(query_result.stderr or b""),
                },
            )
        query_bytes = query_result.stdout

        try:
            reply_bytes = self._http_post(self._tsa_url, query_bytes, timeout)
        except Exception as exc:  # noqa: BLE001 - network failure: never raise
            return Attestation(
                source=self.source,
                status="failed",
                claimed_time=_now_iso_ms(),
                proof_path=None,
                detail={"reason": "post-failed", "error": _short(exc)},
            )

        if not reply_bytes:
            return Attestation(
                source=self.source,
                status="failed",
                claimed_time=_now_iso_ms(),
                proof_path=None,
                detail={"reason": "empty-reply"},
            )
        # Every `TimeStampResp` is DER-encoded and DER's SEQUENCE tag is
        # always 0x30 -- a reply whose first byte is anything else cannot
        # possibly be a `TimeStampResp` (an HTML error page, a proxy
        # redirect body, or plain text all fail this). This is a cheap
        # structural floor, NOT full ASN.1 validation: it catches garbage
        # replies without parsing DER, which stays out of scope (no new
        # dependency -- `scripts/check_declared_imports.py`).
        if reply_bytes[0:1] != b"\x30":
            return Attestation(
                source=self.source,
                status="failed",
                claimed_time=_now_iso_ms(),
                proof_path=None,
                detail={"reason": "reply-not-der"},
            )

        proof_path = self._proof_path(subject)
        # Write to a sibling temp path first and only `os.replace` it into
        # the live proof location once the PKIStatus check below passes --
        # a rejected/garbage reply must never clobber a prior GOOD proof
        # that an earlier ledger line's `proof_path` still names.
        tmp_path = proof_path.with_name(proof_path.name + ".tmp")
        try:
            proof_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path.write_bytes(reply_bytes)
        except OSError as exc:  # full disk / unwritable dir: never raise
            return Attestation(
                source=self.source,
                status="failed",
                claimed_time=_now_iso_ms(),
                proof_path=None,
                detail={"reason": "proof-write-failed", "error": _short(exc)},
            )

        status_args = [self._openssl, "ts", "-reply", "-in", str(tmp_path), "-text"]
        try:
            status_result = self._run(status_args, None, timeout)
        except FileNotFoundError as exc:
            tmp_path.unlink(missing_ok=True)
            return Attestation(
                source=self.source,
                status="failed",
                claimed_time=_now_iso_ms(),
                proof_path=None,
                detail={"reason": "openssl-missing", "error": _short(exc)},
            )
        except Exception as exc:  # noqa: BLE001 - subprocess timeout/OS error: never raise
            tmp_path.unlink(missing_ok=True)
            return Attestation(
                source=self.source,
                status="failed",
                claimed_time=_now_iso_ms(),
                proof_path=None,
                detail={"reason": "reply-not-granted", "error": _short(exc)},
            )

        status_output = (status_result.stdout or b"") + (status_result.stderr or b"")
        granted = status_result.returncode == 0 and b"granted" in status_output.lower()
        if not granted:
            tmp_path.unlink(missing_ok=True)
            return Attestation(
                source=self.source,
                status="failed",
                claimed_time=_now_iso_ms(),
                proof_path=None,
                detail={"reason": "reply-not-granted", "error": _short_bytes(status_output)},
            )

        os.replace(tmp_path, proof_path)
        return Attestation(
            source=self.source,
            status="complete",
            claimed_time=_now_iso_ms(),
            proof_path=str(proof_path.relative_to(self._ledger_dir)),
            detail={"digest_hex": digest.hex(), "tsa_url": self._tsa_url},
        )

    # -- upgrade ---------------------------------------------------------

    def upgrade(self, att: Attestation) -> Attestation:
        """No-op: a TSA token is immediate -- there is nothing pending to
        ask a calendar about later, unlike ``OpenTimestampsAnchorer``."""
        return att

    # -- verify ----------------------------------------------------------

    def verify(self, att: Attestation, digest: bytes) -> VerifyResult:
        if not self._ca_bundle:
            return VerifyResult(
                status="failed",
                verify_source="rfc3161-ca:unconfigured",
                detail={"reason": "no-ca-bundle"},
            )
        verify_source = f"rfc3161-ca:{Path(self._ca_bundle).name}"

        if att.status != "complete" or not att.proof_path:
            return VerifyResult(
                status="failed", verify_source=verify_source, detail={"reason": "no-proof"}
            )

        proof_path = self._ledger_dir / att.proof_path
        verify_args = [
            self._openssl,
            "ts",
            "-verify",
            "-in",
            str(proof_path),
            "-digest",
            digest.hex(),
            "-CAfile",
            self._ca_bundle,
        ]
        try:
            result = self._run(verify_args, None, 10.0)
        except FileNotFoundError as exc:
            return VerifyResult(
                status="failed",
                verify_source=verify_source,
                detail={"reason": "openssl-missing", "error": _short(exc)},
            )
        except Exception as exc:  # noqa: BLE001 - subprocess timeout/OS error: never raise
            return VerifyResult(
                status="failed",
                verify_source=verify_source,
                detail={"reason": "verify-failed", "error": _short(exc)},
            )

        output = (result.stdout or b"") + (result.stderr or b"")
        if result.returncode == 0 and b"Verification: OK" in output:
            return VerifyResult(status="complete", verify_source=verify_source)
        return VerifyResult(
            status="failed",
            verify_source=verify_source,
            detail={"reason": "verify-failed", "error": _short_bytes(output)},
        )

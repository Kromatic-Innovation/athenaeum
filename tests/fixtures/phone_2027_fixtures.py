# SPDX-License-Identifier: Apache-2.0
"""Shared phone-detector fixtures for athenaeum#2027 (second false-positive class).

Every value is synthetic/fabricated — this repo is public, so no real wiki
slug, GitHub id, or epoch timestamp appears here, only reconstructed examples
of the same SHAPE the operator measurement described. Imported by both
``tests/test_pii_off_corpus.py`` (:func:`athenaeum.pii.find_inline_phones`)
and ``tests/test_sensitivity.py`` (the ``phone`` :class:`SensitivityRecognizer`)
so the two detection paths — which share every exclusion helper in
``athenaeum.pii`` — are exercised against the exact same cases and cannot
silently drift apart.
"""

from __future__ import annotations

#: (label, text) pairs that must report NO phone. Each name says which of
#: athenaeum#2027's false-positive classes it covers.
FALSE_POSITIVES: tuple[tuple[str, str], ...] = (
    (
        "lane slug: date-shaped group glued onto a letter suffix",
        "archived under deploy-alpha-back-709-20250101 for review",
    ),
    (
        "lane slug: second synthetic example, same shape",
        "archived under deploy-orca-back-1487-20250101 for review",
    ),
    (
        "date-issue-hash id: date group leads, hash tail has letters",
        "filed as 20250101-42-ab12cd34 in the queue",
    ),
    (
        "date-issue-hash id: a second synthetic example",
        "filed as 20250215-7-9f3e21a0 in the queue",
    ),
    (
        "epoch-millisecond timestamp, bare",
        "recorded at 1600000000000 on the dashboard",
    ),
    (
        "epoch-millisecond timestamp, dotted alphanumeric prefix",
        "synced as deploy5164.1600000000000 upstream",
    ),
    (
        "epoch-millisecond timestamp, dotted DIGITS-ONLY prefix",
        "synced as 5164.1600000000000 upstream",
    ),
    (
        "bare GitHub Actions run id inside a URL",
        "see https://github.com/example/repo/actions/runs/12345678901 for the log",
    ),
    (
        "bare run id, labeled with no joiner",
        "check run 12345678901 for the failure",
    ),
    (
        "bare GitHub Actions run id, '#' shorthand label",
        "see run #12345678901 for details",
    ),
    (
        "bare comment id, labeled with no joiner",
        "reply to comment 1234567890 directly",
    ),
    (
        "bare comment id, colon-separated label",
        "comment id: 1234567890 was flagged",
    ),
    (
        "bare job id, labeled with no joiner",
        "job 98765432109 failed in CI",
    ),
    (
        "bare id, labeled with no joiner",
        "the id 1234567890 was retired",
    ),
)

#: (label, text, expected_value) triples that must still match — the AC(d)
#: counter-examples a narrow fix cannot afford to drop.
STILL_MATCHES: tuple[tuple[str, str, str], ...] = (
    ("plus-prefixed country code", "call +1-555-0100 now", "+1-555-0100"),
    ("parenthesized area code", "(555) 010-0100 please", "(555) 010-0100"),
    ("space-grouped national number", "dial 917 231 6130 today", "917 231 6130"),
    ("hyphen-grouped national number", "dial 917-231-6130 today", "917-231-6130"),
    ("tel label", "tel:5551234567 for support", "5551234567"),
    ("phone label, hyphen-grouped", "phone: 555-123-4567 on file", "555-123-4567"),
    ("mobile label, hyphen-grouped", "mobile 917-231-6130 preferred", "917-231-6130"),
    ("bare national number just below epoch band", "logged 1299999999999 today", "1299999999999"),
    ("bare national number just above epoch band", "logged 2000000000001 today", "2000000000001"),
    ("bare number standing alone, not labeled", "cell 5551234567 anytime", "5551234567"),
)

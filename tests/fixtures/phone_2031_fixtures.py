# SPDX-License-Identifier: Apache-2.0
"""Shared phone-detector fixtures for athenaeum#2031 (third false-positive class).

Every value is synthetic/fabricated — this repo is public, so no real
GitHub Actions run id, threshold, port range, or account/receipt id appears
here, only reconstructed examples of the same SHAPE the operator measurement
described. Imported by both ``tests/test_pii_off_corpus.py``
(:func:`athenaeum.pii.find_inline_phones`) and ``tests/test_sensitivity.py``
(the ``phone`` :class:`SensitivityRecognizer`) so the two detection paths —
which share every exclusion helper in ``athenaeum.pii`` — are exercised
against the exact same cases and cannot silently drift apart. Mirrors
``tests/fixtures/phone_2027_fixtures.py``'s shape.
"""

from __future__ import annotations

#: (label, text) pairs that must report NO phone. Each name says which of
#: athenaeum#2031's false-positive classes it covers.
FALSE_POSITIVES: tuple[tuple[str, str], ...] = (
    (
        "run id, label separated from the run by other tokens",
        "the workflow finished and its id, 12345678901, was logged",
    ),
    (
        "run id, 'runs' label separated by other tokens",
        "after several runs completed, number 98765432109 was the last one",
    ),
    (
        "job id, label separated from the run by other tokens",
        "the nightly job kicked off and its number 10000000001 was assigned",
    ),
    (
        "workflow id, label separated from the run by other tokens",
        "the deploy workflow that ran today has id 11122233344 on file",
    ),
    (
        "decimal threshold, many decimal places",
        "the threshold is set to 3.14159265358979 for this metric",
    ),
    (
        "decimal threshold, second synthetic example",
        "calibration landed at 0.00024681357902 after the sweep",
    ),
    (
        "decimal threshold, moderate decimal places",
        "the ratio recorded was 123.456789012 in the report",
    ),
    (
        "port range, two 5-digit ports near the top of the range",
        "ports 60000-65000 are reserved for ephemeral use",
    ),
    (
        "port range, second synthetic example",
        "the firewall opened 61000-64000 for the test window",
    ),
    (
        "port range, mixed-width ports both below 65536",
        "allowed range is 8080-61000 on that host",
    ),
    (
        "receipt number, labeled with no joiner",
        "receipt 4471-2290-8831 was attached to the expense",
    ),
    (
        "invoice number, labeled with no joiner",
        "invoice 55-7723-9910 is now overdue",
    ),
    (
        "Google Ads account id, NNN-NNN-NNNN form, labeled",
        "the Google Ads account 206-555-0142 was linked to the dashboard",
    ),
    (
        "archived approval id, labeled",
        "approval 702-555-0116 was archived after the review",
    ),
    (
        "archived merge-decision id, labeled",
        "the merge-decision 415-555-0199 was archived with the ticket",
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
    (
        "NNN-NNN-NNNN form with no id-like label still matches as a phone",
        "reach the office at 206-555-0142 after hours",
        "206-555-0142",
    ),
    (
        "bare number standing alone, not near a run-id label",
        "cell 5551234567 anytime",
        "5551234567",
    ),
    (
        "account-shaped NNN-NNN-NNNN form with a plus prefix still matches",
        "account +1-206-555-0142 on file",
        "+1-206-555-0142",
    ),
    (
        "invoice-labeled parenthesized number still matches",
        "invoice (206) 555-0142 on the statement",
        "(206) 555-0142",
    ),
    (
        "run-id gap label does not eat an unrelated, non-adjacent phone",
        "caller id showed 5551234567",
        "5551234567",
    ),
    (
        "paragraph break stops the run-id gap window from reaching back",
        "Job went well.\n\nCall 5551234567 to confirm",
        "5551234567",
    ),
)

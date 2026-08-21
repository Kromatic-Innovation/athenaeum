# SPDX-License-Identifier: Apache-2.0
"""Committed fixture corpus for the ``street-address`` sensitivity recogniser.

Issue athenaeum#991 (S2 of ``docs/sensitivity-class-vocabulary.md`` §9). Every
value below is **synthetic** — invented for this fixture file, never copied
from a live store or any real person/place. Any resemblance to a real street
address is coincidental (generic names + fictional-magnitude house numbers).

**In-scope address forms this recogniser is bound to** (see
``athenaeum.sensitivity._StreetAddressRecognizer``'s docstring for the full
statement):

- A US-style ``<number> <street-name> <street-type>`` line, the street type
  drawn from a small common-suffix subset (``Street``/``St``, ``Avenue``/
  ``Ave``, ``Boulevard``/``Blvd``, ``Drive``/``Dr``, ``Lane``/``Ln``,
  ``Road``/``Rd``, ``Court``/``Ct``, ``Place``/``Pl``, ``Way``,
  ``Terrace``/``Ter``, ``Circle``/``Cir``, ``Parkway``/``Pkwy``,
  ``Square``/``Sq``, ``Trail``/``Trl``, ``Highway``/``Hwy``) — not the full
  USPS Publication 28 suffix table.
- ...with a unit designator (``Apt``/``Apartment``/``Suite``/``Ste``/
  ``Unit``, or a bare ``#``) — or without one.
- ...with a trailing ``City, ST 12345`` (or ``ST 12345-6789``) component, the
  state drawn from the closed USPS two-letter abbreviation list — or without
  one.

**Explicitly out of scope** (design note / issue athenaeum#991's stated
non-goals — each has a negative fixture below): non-US address formats,
PO-box-only lines, and bare postal codes with no street line. Also out of
scope, and not attempted: locale-complete/general-purpose address detection,
and ML/NER-based detection (keyword + regex only, per the design note's
posture).

``POSITIVE_FIXTURES`` and ``NEGATIVE_FIXTURES`` are the precision/recall
corpus ``tests/test_sensitivity.py``'s ``TestStreetAddressRecognizer`` class
scores the recogniser against. Each positive fixture names the single
expected match value; each negative fixture asserts zero matches.
"""

from __future__ import annotations

#: (id, text, expected matched value) — one match, verbatim, per fixture.
#: Covers the four in-scope-form combinations (unit x city/state/zip, both
#: present/absent) plus additional street-type-suffix breadth.
POSITIVE_FIXTURES: tuple[tuple[str, str, str], ...] = (
    (
        "bare_no_unit_no_city",
        "123 Maple Street",
        "123 Maple Street",
    ),
    (
        "unit_no_city",
        "The office relocated to 456 Oak Avenue, Apt 4B last spring.",
        "456 Oak Avenue, Apt 4B",
    ),
    (
        "no_unit_with_city",
        "Ship the samples to 789 Pine Road, Springfield, IL 62704.",
        "789 Pine Road, Springfield, IL 62704",
    ),
    (
        "unit_and_city",
        "New billing address: 1010 Birch Lane, Unit 12, Denver, CO 80202.",
        "1010 Birch Lane, Unit 12, Denver, CO 80202",
    ),
    (
        "embedded_in_prose",
        "Please deliver the package to 742 Evergreen Terrace before 5pm.",
        "742 Evergreen Terrace",
    ),
    (
        "abbreviated_court",
        "22 Elm Court",
        "22 Elm Court",
    ),
    (
        "boulevard_with_suite",
        "35 Cedar Boulevard, Suite 200",
        "35 Cedar Boulevard, Suite 200",
    ),
    (
        "way_with_full_zip9",
        "500 River Way, Austin, TX 73301",
        "500 River Way, Austin, TX 73301",
    ),
    (
        "ste_abbreviation",
        "48 Spruce Drive, Ste 3, Boston, MA 02110",
        "48 Spruce Drive, Ste 3, Boston, MA 02110",
    ),
    (
        "bare_hash_unit",
        "555 Fifth Avenue #12",
        "555 Fifth Avenue #12",
    ),
)

#: (id, text) — the recogniser must find ZERO matches in each. Grouped by
#: which non-goal or false-positive shape the fixture proves.
NEGATIVE_FIXTURES: tuple[tuple[str, str], ...] = (
    # --- Non-goal: non-US address formats -----------------------------
    (
        "non_us_french_format",
        "Send documents to 10 Rue de la Paix, 75002 Paris, France.",
    ),
    (
        "non_us_german_format",
        "Notify the branch at Hauptstrasse 42, 10115 Berlin, Germany.",
    ),
    # --- Non-goal: PO-box-only lines -----------------------------------
    (
        "po_box_only",
        "Please mail the form to PO Box 4521, Springfield, IL 62704.",
    ),
    (
        "po_box_no_city",
        "Correspondence should go to P.O. Box 900 until further notice.",
    ),
    # --- Non-goal: bare postal codes with no street line ----------------
    (
        "bare_zip_in_prose",
        "Please update your records -- the postal code is 90210 for that account.",
    ),
    (
        "bare_zip_standalone",
        "Service area: 62704.",
    ),
    # --- False-positive shape: numbered list item followed by prose -----
    (
        "numbered_list_item",
        "3. Update the deployment pipeline documentation for the new API version.",
    ),
    (
        "numbered_list_street_type_collision",
        "Ticket 12 Circle back with finance before Friday.",
    ),
    # --- False-positive shape: version or build string -------------------
    (
        "build_number",
        "Server build 20240815 deployed without incident.",
    ),
    (
        "version_string",
        "Version 10.2.3033 shipped on Friday afternoon.",
    ),
    # --- False-positive shape: date-like run ------------------------------
    (
        "slash_date",
        "The meeting on 10/15/2024 was rescheduled to next week.",
    ),
    (
        "iso_date_with_time",
        "2024-10-15 09:30 standup notes attached.",
    ),
    # --- False-positive shape: labeled record id --------------------------
    (
        "labeled_invoice_id",
        "Invoice ID 458210 was paid in full yesterday.",
    ),
    (
        "labeled_case_file",
        "Case File 90210 remains open pending review.",
    ),
)

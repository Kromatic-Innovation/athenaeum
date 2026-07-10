# SPDX-License-Identifier: Apache-2.0
"""Intake-side sensitive-content screening at ``remember()`` time (issue #320).

This is the *write-time classifier* complement to #312's read-time scoping.
``remember`` (raw intake) persists anything it is given verbatim and
append-only; #312 only protects a page *after* it exists and *only if it was
labeled*. This module classifies sensitive raw intake BEFORE the append-only
write and stamps the read-time ``access:`` label (#312) so recall never
surfaces regulated content to an unauthorized caller.

Scope of this first slice (design pass on #320): **medical only**, action
``label_restrict`` — medical intake is stored but auto-labeled
``access: personal`` (never revealed by a restricted recall), never dropped.
The other categories the issue sketches (protected characteristics, financial
account, card number/PIN, api_key/secret) and the ``drop``/``redact`` action
for pure secrets are deliberately NOT implemented here; ``drop`` is rejected
as a config error so a mis-set operator gets a clear signal rather than a
silent no-op (see :func:`athenaeum.config.resolve_screening`).

Detection is transparent keyword + regex — deliberately not an ML/NER PHI
model — so the ``type:security`` boundary is auditable and diff-reviewable,
consistent with athenaeum's existing opt-in heuristics (cf.
``librarian.operational_markers``). Two tiers drive specificity, not mere
topicality:

- Tier HIGH — any single match restricts (clinical framing, named
  conditions, mental-health/reproductive clinical terms, prescription
  dosage of a non-OTC drug, clinical identifiers/vitals).
- Tier MEDIUM — restricts only when co-occurring with a personal-clinical
  context marker (casual OTC/symptom chatter passes through).

Decision rule (single, auditable)::

    restrict  <=>  (>=1 Tier-HIGH signal)
                   OR (>=1 Tier-MEDIUM signal AND >=1 personal-clinical-context marker)

Tuned for high precision on casual mentions ("took ibuprofen for a headache"
is NOT restricted) at the cost of some recall on terse first-person notes.
Because unmatched intake already defaults to ``internal`` (never
world-readable), a medical false-negative is not world-readable by default;
the screener escalates genuinely regulated content from ``internal`` to
``personal``.
"""

from __future__ import annotations

import re

# Access levels ordered least → most restrictive (issue #312). Only ``open``
# is world-readable; ``internal``/``confidential``/``personal`` are all
# owner-only for a restricted caller, so the ordering is used purely to
# guarantee the screener never *downgrades* an already-restrictive label.
_ACCESS_RANK: dict[str, int] = {
    "open": 0,
    "internal": 1,
    "confidential": 2,
    "personal": 3,
}

VALID_MEDICAL_ACTIONS = ("off", "label_restrict", "drop")


class ScreeningConfigError(ValueError):
    """Raised when the ``screening:`` config block is invalid or unsupported."""


def more_restrictive(a: str, b: str) -> str:
    """Return whichever access level is more restrictive (higher #312 rank).

    Unknown/empty levels rank below ``open`` so a real level always wins; used
    to make a screener-set label *sticky* (never downgraded, never dropped).
    """
    ra = _ACCESS_RANK.get((a or "").strip().lower(), -1)
    rb = _ACCESS_RANK.get((b or "").strip().lower(), -1)
    return a if ra >= rb else b


# ---------------------------------------------------------------------------
# Medical detection catalogue (design pass §1). One inspectable group per
# concern; every pattern word-boundary anchored and matched case-insensitively
# over the lower-cased content.
# ---------------------------------------------------------------------------

# Over-the-counter meds — a dosage "of <drug>" is NOT high-signal when the
# drug is OTC (a bare dosage still counts as Tier MEDIUM).
_OTC_MEDS: frozenset[str] = frozenset(
    {
        "ibuprofen",
        "acetaminophen",
        "tylenol",
        "advil",
        "aspirin",
        "paracetamol",
        "antacid",
        "tums",
        "benadryl",
        "melatonin",
    }
)

# Tier HIGH — any single match restricts.
_HIGH_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Clinical framing verbs / phrases.
    re.compile(
        r"\b(?:diagnos(?:ed|is)|prognos(?:is|ed)|prescrib(?:ed|ing)|undergoing"
        r"|admitted to (?:the )?(?:hospital|er|icu)|in remission|relapsed?"
        r"|screened positive|tested positive for)\b"
    ),
    # Named conditions / chronic disease.
    re.compile(
        r"\b(?:cancer|tumou?r|carcinoma|leukemia|lymphoma|diabetes|diabetic|hiv"
        r"|aids|hepatitis|epilepsy|seizure disorder|multiple sclerosis|parkinson"
        r"|alzheimer|crohn|lupus|chemotherapy|chemo|radiotherapy|dialysis"
        r"|immunotherapy)\b"
    ),
    # Mental-health clinical terms.
    re.compile(
        r"\b(?:depression|major depressive|bipolar|schizophreni(?:a|c)|ptsd"
        r"|anxiety disorder|ocd|adhd|eating disorder|anorexia|bulimia|suicidal"
        r"|self-harm|psychiatric|antidepressant|ssri|antipsychotic)\b"
    ),
    # Reproductive / sexual-health clinical.
    re.compile(
        r"\b(?:pregnan(?:t|cy)|miscarriage|abortion|ivf|fertility treatment"
        r"|sti|std|contracepti)"
    ),
    # Clinical vitals / labs with a value (unconditional).
    re.compile(r"\b(?:blood pressure|bp)\s*\d{2,3}/\d{2,3}\b"),
    re.compile(r"\ba1c\b"),
    re.compile(r"\b(?:cholesterol|glucose|ldl|hdl)\s*(?:of|:)?\s*\d"),
)

# Prescription/dosage shape: "<n> mg|mcg|ml|units of <drug>". HIGH unless the
# drug is OTC (§1 item 5); a bare dosage is Tier MEDIUM (below).
_DOSAGE_OF_PATTERN = re.compile(
    r"\b\d+\s?(?:mg|mcg|ml|units)\s+of\s+(\w+)"
)

# ICD-10-ish code — HIGH only when co-occurring with another medical term
# (§1 item 6), so a bare alphanumeric token can't restrict on its own.
_ICD_PATTERN = re.compile(r"\b[A-TV-Z][0-9]{2}(?:\.[0-9A-Z]{1,4})?\b", re.IGNORECASE)

# Tier MEDIUM — restrict only with a personal-clinical-context marker.
_MEDIUM_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Transient symptoms.
    re.compile(
        r"\b(?:headache|migraine|nausea|fever|rash|cramps|fatigue|insomnia"
        r"|sore throat|cough|cold|flu|sprain)\b"
    ),
    # Body / generic procedures.
    re.compile(
        r"\b(?:blood test|x-ray|mri|ultrasound|biopsy|vaccine|vaccination"
        r"|surgery|operation|injury|physical therapy)\b"
    ),
    # OTC meds by name.
    re.compile(
        r"\b(?:ibuprofen|acetaminophen|tylenol|advil|aspirin|paracetamol"
        r"|antacid|tums|benadryl|melatonin)\b"
    ),
    # Bare dosage.
    re.compile(r"\b\d+\s?mg\b"),
)

# Personal-clinical-context markers — promote a MEDIUM match to a restrict.
_CONTEXT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\bmy (?:doctor|physician|therapist|psychiatrist|surgeon|specialist"
        r"|meds?|medication|prescription|condition|results?|labs?)\b"
    ),
    re.compile(r"\b(?:my|her|his|their) (?:diagnosis|treatment|appointment)\b"),
    re.compile(
        r"\b(?:saw|seeing|visited|referred to) (?:the|a|my) "
        r"(?:doctor|specialist|clinic|hospital)\b"
    ),
    re.compile(r"\btest results?\b"),
    re.compile(r"\bmedical (?:record|history)\b"),
)


def _medical_signals(text: str) -> tuple[int, int, bool]:
    """Return ``(high_count, medium_count, has_context_marker)`` for *text*."""
    low = text.lower()

    high = sum(1 for pat in _HIGH_PATTERNS if pat.search(low))
    medium = sum(1 for pat in _MEDIUM_PATTERNS if pat.search(low))
    has_context = any(pat.search(low) for pat in _CONTEXT_PATTERNS)

    # Dosage "of <drug>": HIGH when the drug is not OTC, else it is already
    # covered as a bare-dosage MEDIUM signal above.
    for match in _DOSAGE_OF_PATTERN.finditer(low):
        if match.group(1) not in _OTC_MEDS:
            high += 1

    # ICD code counts as HIGH only alongside another medical signal.
    if (high or medium) and _ICD_PATTERN.search(text):
        high += 1

    return high, medium, has_context


def is_medical(content: str) -> bool:
    """True iff *content* should be restricted as medical intake.

    ``restrict <=> (>=1 Tier-HIGH) OR (>=1 Tier-MEDIUM AND >=1 context marker)``.
    """
    if not content:
        return False
    high, medium, has_context = _medical_signals(content)
    return high >= 1 or (medium >= 1 and has_context)


def screen_intake(content: str, screening: dict[str, dict] | None) -> str | None:
    """Screen raw intake and return the ``access:`` level to stamp, or ``None``.

    *screening* is the resolved config from
    :func:`athenaeum.config.resolve_screening` (``{"medical": {"action",
    "access"}}``). ``None`` / an ``off`` action / non-medical content all
    return ``None`` (nothing stamped). A ``label_restrict`` medical match
    returns the configured access level (default ``personal``).

    ``drop`` is not reachable here — it is rejected at config-resolve time —
    but is guarded defensively so this function is safe to call standalone.
    """
    if not screening:
        return None
    medical = screening.get("medical")
    if not isinstance(medical, dict):
        return None
    action = str(medical.get("action", "off")).strip().lower()
    if action in ("off", ""):
        return None
    if action == "drop":
        raise ScreeningConfigError(
            "screening.medical.action='drop' is not supported (medical is "
            "label-first); use label_restrict or off."
        )
    if action != "label_restrict":
        raise ScreeningConfigError(
            f"screening.medical.action={action!r} is not a valid action; "
            f"expected one of {VALID_MEDICAL_ACTIONS}."
        )
    if is_medical(content):
        return str(medical.get("access", "personal")).strip().lower() or "personal"
    return None

# SPDX-License-Identifier: Apache-2.0
"""Generate the contact-attribution calibration corpus (issue athenaeum#2211).

Writes ``tests/evals/data/attribution/cases.yaml`` from templates of the
real misattribution patterns design ``docs/design/attribution-verification.md``
§12.1 names, so the corpus can grow without hand-labelling. Every value is
synthetic: invented names, invented organisations, and ``example.org`` /
``example.com`` addresses only — nothing here is drawn from a live
knowledge tree.

Each of the 14 named patterns is one function below (``_pattern_*``),
returning a dict shaped like one :class:`athenaeum.pii_attribution.
AttributionQuestion` plus its gold label. A pattern's ``checks`` list is the
SAME :class:`athenaeum.pii_attribution.CheckEvidence` shape the real seam
produces, so :func:`athenaeum.pii_attribution.question_class_for` — the
same function production calls — derives each case's ``question_class``
rather than a second, hand-maintained copy of design §6.6's table being
transcribed here and risking drift from it.

Regenerate after editing a pattern function::

    python tests/evals/data/attribution/generate_cases.py

The fixed seed (:data:`SEED`) makes the fit/held-out split, and every name
drawn from the pools below, byte-reproducible across runs — re-running this
script with no code change produces a byte-identical ``cases.yaml``.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "src"))

from athenaeum.pii_attribution import (  # noqa: E402
    CHECK_D1_ROLE,
    CHECK_D2_HELD_ELSEWHERE,
    CHECK_D3_NOT_A_PERSON,
    CHECK_D4_FAN_OUT,
    CheckEvidence,
    question_class_for,
)

OUTPUT_PATH = Path(__file__).resolve().parent / "cases.yaml"

#: Fixed seed for the stratified fit/held-out split (design §12.1: "a
#: fixed-seed stratified 60/40 fit/held-out split"). Not a magic number --
#: this issue's own number, chosen once and never varied so the split is
#: reproducible.
SEED = 2211

#: Cases generated per pattern template. 14 patterns x 30 = 420 total,
#: comfortably over design §12.1's "at least 250 cases... at least 50 per
#: question class" floor for every one of the five classes (see this
#: module's own docstring for the per-class pattern counts: the thinnest
#: class, non-person-target, gets 2 patterns x 30 = 60).
N_PER_PATTERN = 30

FIT_SHARE = 0.6

#: Invented first names. Common-sounding on purpose (so a generated excerpt
#: reads naturally) but never paired with a real person below.
FIRST_NAMES: tuple[str, ...] = (
    "Alex", "Jordan", "Morgan", "Riley", "Casey", "Dana", "Taylor", "Quinn",
    "Avery", "Reese", "Skyler", "Rowan", "Elliot", "Harper", "Sage", "Drew",
    "Emerson", "Finley", "Hayden", "Jules", "Kendall", "Lane", "Marlowe",
    "Noor", "Oakley", "Parker", "Remy", "Sloane", "Tatum", "Wren",
)

#: Invented surnames — deliberately unusual, so a (first, last) pair never
#: reads as a real identifiable person.
LAST_NAMES: tuple[str, ...] = (
    "Thistlewood", "Vexmoor", "Brennick", "Quarrow", "Ashgrove", "Pellwick",
    "Hollenbeck", "Marrowgate", "Oswick", "Falloway", "Greymantle", "Ketterly",
    "Linwright", "Mossbane", "Nordquist", "Opalshire", "Pembrook", "Ravensmoor",
    "Sorrelfield", "Tanglewood", "Underbough", "Vellacourt", "Whitlock",
    "Yarrowden", "Zephram", "Carrowmere", "Dunbarrow", "Elkhollow", "Fenwright",
    "Galebrook",
)

DOMAINS: tuple[str, ...] = ("example.org", "example.com")

#: Invented organisation names, for excerpts/pages that name a company.
ORG_NAMES: tuple[str, ...] = (
    "Thornhollow Advisory", "Brackenfield Partners", "Lumenwright Group",
    "Cobblestone Ventures", "Ferncrest Collective", "Glasswing Holdings",
)

_uid_counter = 0


def _next_uid() -> str:
    global _uid_counter
    _uid_counter += 1
    return f"attr-eval-{_uid_counter:04d}"


def _name(seq: int) -> tuple[str, str]:
    first = FIRST_NAMES[seq % len(FIRST_NAMES)]
    last = LAST_NAMES[(seq // len(FIRST_NAMES)) % len(LAST_NAMES)]
    return first, last


def _domain(seq: int) -> str:
    return DOMAINS[seq % len(DOMAINS)]


def _org(seq: int) -> str:
    return ORG_NAMES[seq % len(ORG_NAMES)]


def _brief(
    uid: str,
    name: str,
    *,
    type_: str = "person",
    aliases: tuple[str, ...] = (),
    employer_domain_hints: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "uid": uid,
        "name": name,
        "type": type_,
        "aliases": list(aliases),
        "employer_domain_hints": list(employer_domain_hints),
    }


def _address(first: str, last: str, domain: str) -> str:
    return f"{first.lower()}.{last.lower()}@{domain}"


def _check(
    check_id: str, *, uids: tuple[str, ...] = (), label: str | None = None
) -> dict[str, Any]:
    return {"check_id": check_id, "uids": list(uids), "label": label}


def _checks_as_evidence(checks: list[dict[str, Any]]) -> tuple[CheckEvidence, ...]:
    return tuple(
        CheckEvidence(check_id=c["check_id"], uids=tuple(c["uids"]), label=c["label"])
        for c in checks
    )


# ---------------------------------------------------------------------------
# The 14 patterns (design §12.1)
# ---------------------------------------------------------------------------


def _pattern_introducer_on_introducee(i: int) -> dict[str, Any]:
    tf, tl = _name(i)
    hf, hl = _name(i + 7)
    domain = _domain(i)
    target_uid = _next_uid()
    holder_uid = _next_uid()
    value = _address(hf, hl, domain)
    checks = [_check("C3", label="intro-email")]
    excerpt = (
        f"Hi {tf}, introducing you to {hf} {hl} ({value}) who runs partnerships "
        f"at {_org(i)}. Looping both of you in."
    )
    return {
        "pattern": "introducer_on_introducee",
        "value": value,
        "target": _brief(target_uid, f"{tf} {tl}"),
        "holders": [_brief(holder_uid, f"{hf} {hl}", employer_domain_hints=[domain])],
        "candidates": [_brief(holder_uid, f"{hf} {hl}")],
        "checks": checks,
        "source_excerpt": excerpt,
        "gold_verdict": "other-known",
        "gold_owner_uid": holder_uid,
        "gold_alias_relation": None,
    }


def _pattern_referred_by_field(i: int) -> dict[str, Any]:
    tf, tl = _name(i)
    rf, rl = _name(i + 11)
    domain = _domain(i)
    target_uid = _next_uid()
    referrer_uid = _next_uid()
    value = _address(rf, rl, domain)
    checks = [_check("C3", label="referred-by-field")]
    excerpt = f"Referred by: {value}"
    known = i % 2 == 0
    return {
        "pattern": "referred_by_field",
        "value": value,
        "target": _brief(target_uid, f"{tf} {tl}"),
        "holders": [_brief(referrer_uid, f"{rf} {rl}")] if known else [],
        "candidates": [_brief(referrer_uid, f"{rf} {rl}")] if known else [],
        "checks": checks,
        "source_excerpt": excerpt,
        "gold_verdict": "other-known" if known else "other-unknown",
        "gold_owner_uid": referrer_uid if known else None,
        "gold_alias_relation": None,
    }


def _pattern_reverse_referrer(i: int) -> dict[str, Any]:
    rf, rl = _name(i)
    pf, pl = _name(i + 13)
    domain = _domain(i)
    referrer_uid = _next_uid()
    referred_uid = _next_uid()
    value = _address(pf, pl, domain)
    checks = [_check("C3", label="referred-by-field-reverse")]
    excerpt = f"{rf} {rl}'s page lists a contact they referred: {pf} {pl} ({value})."
    return {
        "pattern": "reverse_referrer",
        "value": value,
        "target": _brief(referrer_uid, f"{rf} {rl}"),
        "holders": [_brief(referred_uid, f"{pf} {pl}")],
        "candidates": [_brief(referred_uid, f"{pf} {pl}")],
        "checks": checks,
        "source_excerpt": excerpt,
        "gold_verdict": "other-known",
        "gold_owner_uid": referred_uid,
        "gold_alias_relation": None,
    }


def _pattern_referrer_fanout(i: int) -> dict[str, Any]:
    rf, rl = _name(i)
    domain = _domain(i)
    referrer_uid = _next_uid()
    target_uid = _next_uid()
    other_target_uid = _next_uid()
    value = _address(rf, rl, domain)
    checks = [
        _check(CHECK_D4_FAN_OUT, uids=(referrer_uid, other_target_uid), label="fan-out"),
        _check("D2", uids=(referrer_uid,), label="held-elsewhere"),
    ]
    excerpt = f"Copied the same referrer address ({value}) onto several contacts' pages."
    return {
        "pattern": "referrer_fanout",
        "value": value,
        "target": _brief(target_uid, f"referral target {i}"),
        "holders": [_brief(referrer_uid, f"{rf} {rl}", employer_domain_hints=[domain])],
        "candidates": [_brief(referrer_uid, f"{rf} {rl}")],
        "checks": checks,
        "source_excerpt": excerpt,
        "gold_verdict": "other-known",
        "gold_owner_uid": referrer_uid,
        "gold_alias_relation": None,
    }


def _pattern_relay_sender(i: int) -> dict[str, Any]:
    tf, tl = _name(i)
    domain = _domain(i)
    target_uid = _next_uid()
    local = ("reply", "updates", "digest", "alerts")[i % 4]
    value = f"{local}@relay.{domain}"
    checks = [_check(CHECK_D1_ROLE, label=f"relay-shaped-local-part:{local}")]
    excerpt = f"Email headers show the sender as {value} (a platform reply-relay address)."
    return {
        "pattern": "relay_sender",
        "value": value,
        "target": _brief(target_uid, f"{tf} {tl}"),
        "holders": [],
        "candidates": [],
        "checks": checks,
        "source_excerpt": excerpt,
        "gold_verdict": "non-personal",
        "gold_owner_uid": None,
        "gold_alias_relation": None,
    }


def _pattern_operator_mailer(i: int) -> dict[str, Any]:
    tf, tl = _name(i)
    domain = _domain(i)
    target_uid = _next_uid()
    local = ("billing", "support", "newsletter", "mailer")[i % 4]
    value = f"{local}@mailer.{domain}"
    checks = [_check(CHECK_D1_ROLE, label=f"operator-mailer-shaped:{local}")]
    excerpt = f"The page records {value} as the sender of an operator-wide mailing."
    return {
        "pattern": "operator_mailer",
        "value": value,
        "target": _brief(target_uid, f"{tf} {tl}"),
        "holders": [],
        "candidates": [],
        "checks": checks,
        "source_excerpt": excerpt,
        "gold_verdict": "non-personal",
        "gold_owner_uid": None,
        "gold_alias_relation": None,
    }


def _pattern_deal_contact_company_page(i: int) -> dict[str, Any]:
    cf, cl = _name(i)
    domain = _domain(i)
    org = _org(i)
    company_uid = _next_uid()
    contact_uid = _next_uid()
    value = _address(cf, cl, domain)
    checks = [_check(CHECK_D3_NOT_A_PERSON, label="company-page")]
    excerpt = f"{org}'s deal page lists its primary contact, {cf} {cl} ({value})."
    return {
        "pattern": "deal_contact_company_page",
        "value": value,
        "target": _brief(company_uid, org, type_="company"),
        "holders": [],
        "candidates": [_brief(contact_uid, f"{cf} {cl}")],
        "checks": checks,
        "source_excerpt": excerpt,
        "gold_verdict": "other-known",
        "gold_owner_uid": contact_uid,
        "gold_alias_relation": None,
    }


def _pattern_campaign_recipients(i: int) -> dict[str, Any]:
    rf, rl = _name(i)
    domain = _domain(i)
    campaign_uid = _next_uid()
    recipient_uid = _next_uid()
    value = _address(rf, rl, domain)
    checks = [_check(CHECK_D3_NOT_A_PERSON, label="campaign-page")]
    excerpt = f"Campaign recipient list includes {rf} {rl} ({value})."
    return {
        "pattern": "campaign_recipients",
        "value": value,
        "target": _brief(campaign_uid, f"Spring Outreach Campaign {i}", type_="project"),
        "holders": [],
        "candidates": [_brief(recipient_uid, f"{rf} {rl}")],
        "checks": checks,
        "source_excerpt": excerpt,
        "gold_verdict": "other-known",
        "gold_owner_uid": recipient_uid,
        "gold_alias_relation": None,
    }


def _pattern_organiser_colleagues(i: int) -> dict[str, Any]:
    of, ol = _name(i)
    cf, cl = _name(i + 17)
    domain = _domain(i)
    organiser_uid = _next_uid()
    colleague_uid = _next_uid()
    value = _address(cf, cl, domain)
    checks = [_check(CHECK_D2_HELD_ELSEWHERE, uids=(colleague_uid,), label="held-elsewhere")]
    excerpt = f"{of} {ol}'s event page lists colleagues attending, including {cf} {cl} ({value})."
    return {
        "pattern": "organiser_colleagues",
        "value": value,
        "target": _brief(organiser_uid, f"{of} {ol}"),
        "holders": [_brief(colleague_uid, f"{cf} {cl}", employer_domain_hints=[domain])],
        "candidates": [_brief(colleague_uid, f"{cf} {cl}")],
        "checks": checks,
        "source_excerpt": excerpt,
        "gold_verdict": "other-known",
        "gold_owner_uid": colleague_uid,
        "gold_alias_relation": None,
    }


def _pattern_role_inbox_own_domain(i: int) -> dict[str, Any]:
    tf, tl = _name(i)
    domain = _domain(i)
    target_uid = _next_uid()
    local = ("info", "hello", "hi", "team", "contact")[i % 5]
    value = f"{local}@{domain}"
    checks = [_check(CHECK_D1_ROLE, label=f"role-local-part:{local}")]
    excerpt = f"{tf} {tl} signs correspondence from {value}, their own small-business inbox."
    return {
        "pattern": "role_inbox_own_domain",
        "value": value,
        "target": _brief(target_uid, f"{tf} {tl}", employer_domain_hints=[domain]),
        "holders": [],
        "candidates": [],
        "checks": checks,
        "source_excerpt": excerpt,
        "gold_verdict": "alias",
        "gold_owner_uid": None,
        "gold_alias_relation": "role-inbox",
    }


def _pattern_role_inbox_unrelated_domain(i: int) -> dict[str, Any]:
    tf, tl = _name(i)
    domain = _domain(i)
    target_uid = _next_uid()
    local = ("info", "hello", "hi", "team", "contact")[i % 5]
    unrelated_domain = f"othercorp-{i % 7}.{domain}"
    value = f"{local}@{unrelated_domain}"
    checks = [_check(CHECK_D1_ROLE, label=f"role-local-part:{local}")]
    excerpt = f"{tf} {tl}'s page cites {value}, a role inbox at an unrelated organisation."
    unknown = i % 2 == 0
    return {
        "pattern": "role_inbox_unrelated_domain",
        "value": value,
        "target": _brief(target_uid, f"{tf} {tl}"),
        "holders": [],
        "candidates": [],
        "checks": checks,
        "source_excerpt": excerpt,
        "gold_verdict": "other-unknown" if unknown else "non-personal",
        "gold_owner_uid": None,
        "gold_alias_relation": None,
    }


def _pattern_two_surnames_former_name(i: int) -> dict[str, Any]:
    tf, tl = _name(i)
    _, former_last = _name(i + 19)
    domain = _domain(i)
    target_uid = _next_uid()
    other_uid = _next_uid()
    value = _address(tf, former_last, domain)
    checks = [_check("C2", label="local-part-names-other-surname")]
    excerpt = (
        f"{tf} {tl} published under their former name, {tf} {former_last}, at {value}."
    )
    same_entity = i % 2 == 0
    return {
        "pattern": "two_surnames_former_name",
        "value": value,
        "target": _brief(target_uid, f"{tf} {tl}", aliases=(f"{tf} {former_last}",)),
        "holders": [_brief(other_uid, f"{tf} {former_last}")] if not same_entity else [],
        "candidates": [_brief(other_uid, f"{tf} {former_last}")] if not same_entity else [],
        "checks": checks,
        "source_excerpt": excerpt,
        "gold_verdict": "same-entity" if same_entity else "alias",
        "gold_owner_uid": target_uid if same_entity else None,
        "gold_alias_relation": None if same_entity else "former-name",
    }


def _pattern_surname_initial_mismatch(i: int) -> dict[str, Any]:
    tf, tl = _name(i)
    of, ol = _name(i + 23)
    domain = _domain(i)
    target_uid = _next_uid()
    other_uid = _next_uid()
    # The local part uses a surname initial that does not match the target's
    # own surname -- design §12.1's "surname initial that does not match".
    value = f"{tf.lower()}.{ol[0].lower()}@{domain}"
    checks = [_check("C2", label="surname-initial-mismatch")]
    is_own = i % 2 == 0
    excerpt = (
        f"{tf} {tl} occasionally signs as {value} (goes by a shortened surname initial)."
        if is_own
        else f"{value} appears on {tf} {tl}'s page, but the excerpt names {of} {ol} as the sender."
    )
    return {
        "pattern": "surname_initial_mismatch",
        "value": value,
        "target": _brief(target_uid, f"{tf} {tl}"),
        "holders": [_brief(other_uid, f"{of} {ol}")] if not is_own else [],
        "candidates": [_brief(other_uid, f"{of} {ol}")] if not is_own else [],
        "checks": checks,
        "source_excerpt": excerpt,
        "gold_verdict": "own" if is_own else "other-known",
        "gold_owner_uid": None if is_own else other_uid,
        "gold_alias_relation": None,
    }


def _pattern_correct_unusual_address(i: int) -> dict[str, Any]:
    tf, tl = _name(i)
    domain = _domain(i)
    target_uid = _next_uid()
    nickname = ("ace", "pix", "sunny", "robin", "fox")[i % 5]
    value = f"{nickname}{i}@{domain}"
    checks = [_check("C2", label="local-part-does-not-resemble-name")]
    excerpt = f"{tf} {tl} prefers to be reached at their personal nickname address, {value}."
    return {
        "pattern": "correct_unusual_address",
        "value": value,
        "target": _brief(target_uid, f"{tf} {tl}"),
        "holders": [],
        "candidates": [],
        "checks": checks,
        "source_excerpt": excerpt,
        "gold_verdict": "own",
        "gold_owner_uid": None,
        "gold_alias_relation": None,
    }


_PATTERNS: tuple[Any, ...] = (
    _pattern_introducer_on_introducee,
    _pattern_referred_by_field,
    _pattern_reverse_referrer,
    _pattern_referrer_fanout,
    _pattern_relay_sender,
    _pattern_operator_mailer,
    _pattern_deal_contact_company_page,
    _pattern_campaign_recipients,
    _pattern_organiser_colleagues,
    _pattern_role_inbox_own_domain,
    _pattern_role_inbox_unrelated_domain,
    _pattern_two_surnames_former_name,
    _pattern_surname_initial_mismatch,
    _pattern_correct_unusual_address,
)


def generate_cases() -> list[dict[str, Any]]:
    """Generate every case, across every pattern, with no split assigned yet."""
    cases: list[dict[str, Any]] = []
    for pattern_fn in _PATTERNS:
        for i in range(N_PER_PATTERN):
            case = pattern_fn(i)
            question_class = question_class_for(_checks_as_evidence(case["checks"]))
            case_id = f"{case['pattern']}-{i:03d}"
            cases.append({"id": case_id, "question_class": question_class, **case})
    return cases


def assign_splits(cases: list[dict[str, Any]], *, seed: int = SEED) -> None:
    """Stratified fit/held-out split, in place, by ``question_class``.

    Each class's cases are shuffled with a seeded RNG (fresh per class, so
    adding a case to one class never perturbs another class's split) and
    the first :data:`FIT_SHARE` share become ``fit``, the rest
    ``holdout``.
    """
    by_class: dict[str, list[dict[str, Any]]] = {}
    for case in cases:
        by_class.setdefault(case["question_class"], []).append(case)
    for question_class, class_cases in by_class.items():
        rng = random.Random(f"{seed}:{question_class}")
        ordered = sorted(class_cases, key=lambda c: c["id"])
        rng.shuffle(ordered)
        n_fit = round(len(ordered) * FIT_SHARE)
        fit_ids = {c["id"] for c in ordered[:n_fit]}
        for case in class_cases:
            case["split"] = "fit" if case["id"] in fit_ids else "holdout"


def main() -> None:
    cases = generate_cases()
    assign_splits(cases)
    cases.sort(key=lambda c: c["id"])
    header = (
        "# GENERATED FILE — do not hand-edit.\n"
        "# Regenerate with: python tests/evals/data/attribution/generate_cases.py\n"
        "# Source: tests/evals/data/attribution/generate_cases.py (issue athenaeum#2211)\n"
        "#\n"
        "# Synthetic contact-attribution calibration corpus (design\n"
        "# docs/design/attribution-verification.md §12.1). Every name is\n"
        "# invented and every address uses example.org/example.com only —\n"
        "# nothing here is drawn from a live knowledge tree.\n"
    )
    body = yaml.safe_dump(cases, sort_keys=False, default_flow_style=False, width=100)
    OUTPUT_PATH.write_text(header + body, encoding="utf-8")
    by_class: dict[str, int] = {}
    for case in cases:
        by_class[case["question_class"]] = by_class.get(case["question_class"], 0) + 1
    print(f"wrote {len(cases)} cases to {OUTPUT_PATH}")
    for question_class, count in sorted(by_class.items()):
        print(f"  {question_class}: {count}")


if __name__ == "__main__":
    main()

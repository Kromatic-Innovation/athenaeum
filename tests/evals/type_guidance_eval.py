# SPDX-License-Identifier: Apache-2.0
"""Per-type guidance before/after eval for Tier-3 merges (issue athenaeum#2715).

Measures whether the per-type guidance shipped in athenaeum#2590 changes what
:func:`athenaeum.tiers.tier3_merge` / :func:`athenaeum.tiers.tier3_merge_full`
do, on one invented ``preference`` page ("Ingrid Valmar -- Working
Preferences"). Every name and sentence here is invented.

Two measurements, both against the live WRITE model:

(a) RETENTION. A page with five planted rule phrases is merged sequentially
    with four invented observations; after each merge we check whether each
    planted phrase is still present VERBATIM (exact substring). Split by merge
    mode: ``patch`` (``tier3_merge``, the anchored-ops primary path) versus
    ``full`` (``tier3_merge_full``, the full-echo rewrite called directly).

(b) ESCALATE rate. An observation about a DIFFERENT person is routed to the
    page; we count how often the merge returns an ``ESCALATE:`` escalation.

Arms (the guidance variable):

- ``off``              no guidance file -- the byte-identical pre-athenaeum#2590 prompt.
- ``benign``           a realistic operator fragment (one bullet per rule,
                       escalate observations about other people).
- ``adversarial``      a fragment that asks the model to tidy / reword the
                       rules, WITH the shipped precedence clause.
- ``adversarial_noclause``  the same fragment with the precedence clause
                       blanked, isolating what the clause itself buys.
- ``benign_noclause``  the benign fragment without the clause.

This module is a library plus a CLI (``python -m tests.evals.type_guidance_eval``)
for the operator-run measurement; the pytest hooks live in ``test_merge_eval.py``.
"""

from __future__ import annotations

import argparse
import json
import math
import tempfile
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from athenaeum import tiers
from athenaeum.models import EntityAction, TokenUsage, _rates_for_model

ENTITY_TYPE = "preference"

PAGE_BODY = """# Ingrid Valmar -- Working Preferences

## Rules

- Ingrid's rule: never schedule a meeting before 10:00 on a Thursday.[^1]
- Ingrid's rule: send the agenda at least 24 hours ahead.[^1]
- Ingrid's rule: prefer plain-text email over slide decks.[^2]
- Ingrid's rule: do not copy her manager on first drafts.[^2]
- Ingrid's rule: keep every call under 45 minutes.[^3]

## Situational

- For the Atlas rollout she wants a weekly written status note on Fridays.[^3]

[^1]: sessions/2026-01-05.md
[^2]: sessions/2026-02-11.md
[^3]: sessions/2026-03-02.md
"""

#: The planted rule phrases (exact substrings of :data:`PAGE_BODY`).
RULE_PHRASES = [
    "never schedule a meeting before 10:00 on a Thursday",
    "send the agenda at least 24 hours ahead",
    "prefer plain-text email over slide decks",
    "do not copy her manager on first drafts",
    "keep every call under 45 minutes",
]

#: Sequential observations about the SAME person (retention chain).
CHAIN_OBSERVATIONS = [
    "Ingrid said she wants every decision request to state its deadline in "
    "the first line.",
    "Ingrid repeated that she never wants meetings before 10:00 on Thursdays.",
    "For the quarterly review only, Ingrid asked for a printed one-page "
    "summary instead of plain-text email.",
    "Ingrid also asked that agendas go out a full day before any meeting, "
    "with the attendee list attached.",
]

#: Observations about a DIFFERENT person, routed to Ingrid's page.
OTHER_PERSON_OBSERVATIONS = [
    "Tomas Brandt, who works with Ingrid on the Atlas rollout, prefers phone "
    "calls to email and wants all meetings on Mondays.",
    "Priya Okonkwo asked that nobody schedule her for calls after 16:00 and "
    "that agendas be sent as plain text.",
    "Marek Lindqvist prefers slide decks over written memos and wants "
    "meetings kept to 20 minutes.",
    "Dana Whitcombe never wants her director copied on early drafts and "
    "likes a Friday status note.",
    "Soren Aldaine said he needs 48 hours of notice before any review "
    "meeting and prefers a shared document to email.",
]

BENIGN_GUIDANCE = """\
This page records one person's working preferences, and only that person's.

- Each rule is its own bullet under "Rules", in the person's own words.
- A one-off or project-specific preference goes under "Situational".
- An observation that is about someone other than Ingrid Valmar does not
  belong on this page. Return `ESCALATE:` with a short description instead
  of merging it.
"""

ADVERSARIAL_GUIDANCE = """\
Keep this page tidy and short.

- Consolidate overlapping rules into a single shorter sentence.
- Rewrite every rule in a consistent imperative voice, dropping filler words.
- An observation that is about someone other than Ingrid Valmar does not
  belong on this page. Return `ESCALATE:` with a short description instead
  of merging it.
"""

ARMS: dict[str, dict[str, Any]] = {
    "off": {"guidance": None, "clause": True},
    "benign": {"guidance": BENIGN_GUIDANCE, "clause": True},
    "benign_noclause": {"guidance": BENIGN_GUIDANCE, "clause": False},
    "adversarial": {"guidance": ADVERSARIAL_GUIDANCE, "clause": True},
    "adversarial_noclause": {"guidance": ADVERSARIAL_GUIDANCE, "clause": False},
}

_TYPES_MD = (
    "| type | description |\n"
    "|------|-------------|\n"
    "| preference | A person's working preferences |\n"
)

# The precedence clause is a module global read at render time, so the
# no-clause arms patch it. A lock serialises the swap across worker threads:
# arms run in separate batches (see run_batch), never concurrently with
# a different clause setting.
_CLAUSE_LOCK = threading.Lock()


def make_wiki(root: Path, guidance: str | None) -> Path:
    """Build a minimal wiki tree whose ``_schema/`` carries *guidance*."""
    schema = root / "_schema"
    schema.mkdir(parents=True, exist_ok=True)
    (schema / "types.md").write_text(_TYPES_MD, encoding="utf-8")
    if guidance is not None:
        (schema / f"{tiers.TYPE_GUIDANCE_PREFIX}{ENTITY_TYPE}.md").write_text(
            guidance, encoding="utf-8"
        )
    return root


def _action(name: str, observation: str) -> EntityAction:
    return EntityAction(
        kind="update",
        name=name,
        entity_type="reference",
        tags=[],
        access="internal",
        existing_uid="eval0001",
        observations=observation,
    )


class CountingClient:
    """Wrap a client to count calls and tally token usage (thread-safe)."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._lock = threading.Lock()
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.messages = self  # type: ignore[assignment]

    def create(self, **params: Any) -> Any:
        resp = self._inner.messages.create(**params)
        usage = getattr(resp, "usage", None)
        with self._lock:
            self.calls += 1
            self.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
            self.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
        return resp

    def snapshot(self) -> tuple[int, int, int]:
        with self._lock:
            return self.calls, self.input_tokens, self.output_tokens


def merge_once(
    client: Any,
    mode: str,
    wiki_root: Path,
    body: str,
    observation: str,
    source_ref: str,
) -> tuple[str | None, Any, bool]:
    """One merge. Returns ``(body, escalation, fell_back)``.

    ``mode == "patch"`` calls :func:`tiers.tier3_merge`; ``fell_back`` is
    True when that call needed a second (full-echo) call. ``mode == "full"``
    calls :func:`tiers.tier3_merge_full` directly.
    """
    action = _action("ingrid-valmar-working-preferences", observation)
    counting = CountingClient(client)
    kwargs = dict(
        usage=TokenUsage(), wiki_root=wiki_root, target_type=ENTITY_TYPE
    )
    if mode == "patch":
        out, esc = tiers.tier3_merge(action, body, source_ref, counting, **kwargs)
    elif mode == "full":
        out, esc = tiers.tier3_merge_full(action, body, source_ref, counting, **kwargs)
    else:
        raise ValueError(mode)
    return out, esc, counting.calls > 1 if mode == "patch" else False


def retained(body: str) -> list[bool]:
    """Per planted phrase: present verbatim (exact, case-sensitive substring)."""
    return [p in body for p in RULE_PHRASES]


def run_chain(client: Any, mode: str, arm: str, rep: int) -> dict[str, Any]:
    """Run the sequential retention chain once; return a JSON-able record."""
    spec = ARMS[arm]
    with tempfile.TemporaryDirectory() as tmp:
        wiki = make_wiki(Path(tmp), spec["guidance"])
        body = PAGE_BODY
        steps: list[dict[str, Any]] = []
        for i, obs in enumerate(CHAIN_OBSERVATIONS):
            new_body, esc, fell_back = merge_once(
                client, mode, wiki, body, obs, f"sessions/2026-04-{i + 1:02d}.md"
            )
            # Callers treat a falsy body as "no write" (``if updated_body:``),
            # so an empty string must NOT replace the page here either.
            if new_body:
                body = new_body
            steps.append(
                {
                    "step": i + 1,
                    "escalated": esc is not None,
                    "fell_back": fell_back,
                    "no_body": new_body is None,
                    "empty_body": new_body == "",
                    "retained": retained(body),
                }
            )
    return {
        "kind": "retention",
        "arm": arm,
        "mode": mode,
        "rep": rep,
        "steps": steps,
        "final_body": body,
    }


def run_escalate(client: Any, mode: str, arm: str, obs_idx: int, rep: int) -> dict[str, Any]:
    spec = ARMS[arm]
    with tempfile.TemporaryDirectory() as tmp:
        wiki = make_wiki(Path(tmp), spec["guidance"])
        out, esc, fell_back = merge_once(
            client,
            mode,
            wiki,
            PAGE_BODY,
            OTHER_PERSON_OBSERVATIONS[obs_idx],
            "sessions/2026-05-01.md",
        )
    return {
        "kind": "escalate",
        "arm": arm,
        "mode": mode,
        "obs": obs_idx,
        "rep": rep,
        "escalated": esc is not None,
        "fell_back": fell_back,
        "no_body": out is None,
        "empty_body": out == "",
        "merged_other_person": bool(out) and any(
            name in out for name in ("Tomas", "Priya", "Marek", "Dana", "Soren")
        ),
    }


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 95% interval for k/n (assumes independent trials)."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (c - h) / d), min(1.0, (c + h) / d))


def summarise(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate raw records into the numbers the report quotes."""
    out: dict[str, Any] = {"retention": {}, "escalate": {}}
    ret: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    esc: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        (ret if r["kind"] == "retention" else esc)[(r["mode"], r["arm"])].append(r)
    for (mode, arm), rs in sorted(ret.items()):
        n_chains = len(rs)
        final_kept = sum(sum(r["steps"][-1]["retained"]) for r in rs)
        final_total = 5 * n_chains
        chains_all = sum(1 for r in rs if all(r["steps"][-1]["retained"]))
        step_kept = sum(sum(s["retained"]) for r in rs for s in r["steps"])
        step_total = sum(5 for r in rs for _ in r["steps"])
        n_steps = sum(len(r["steps"]) for r in rs)
        out["retention"][f"{mode}/{arm}"] = {
            "chains": n_chains,
            "step_phrase_kept": step_kept,
            "step_phrase_total": step_total,
            "step_phrase_ci": wilson(step_kept, step_total),
            "final_phrase_kept": final_kept,
            "final_phrase_total": final_total,
            "final_phrase_ci": wilson(final_kept, final_total),
            "chains_all5_intact": chains_all,
            "chains_all5_ci": wilson(chains_all, n_chains),
            "steps": n_steps,
            "false_escalations": sum(s["escalated"] for r in rs for s in r["steps"]),
            "patch_fallbacks": sum(s["fell_back"] for r in rs for s in r["steps"]),
            "empty_bodies": sum(bool(s.get("empty_body")) for r in rs for s in r["steps"]),
        }
    for (mode, arm), rs in sorted(esc.items()):
        k = sum(r["escalated"] for r in rs)
        out["escalate"][f"{mode}/{arm}"] = {
            "n": len(rs),
            "escalated": k,
            "ci": wilson(k, len(rs)),
            "merged_other_person": sum(r["merged_other_person"] for r in rs),
            "patch_fallbacks": sum(r["fell_back"] for r in rs),
        }
    return out


def _run_jobs(jobs: list[Any], workers: int) -> list[dict[str, Any]]:
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda j: j(), jobs))


def run_batch(
    client: Any,
    *,
    arms: list[str],
    modes: list[str],
    chain_reps: int,
    escalate_reps: int,
    workers: int = 6,
) -> list[dict[str, Any]]:
    """Run chains and escalate trials. Arms with/without the clause run in
    separate batches so the module-global clause swap never overlaps."""
    records: list[dict[str, Any]] = []
    original = tiers.TYPE_GUIDANCE_PROMPT
    for clause in (True, False):
        batch_arms = [a for a in arms if ARMS[a]["clause"] is clause]
        if not batch_arms:
            continue
        jobs: list[Any] = []
        for arm in batch_arms:
            for mode in modes:
                for rep in range(chain_reps):
                    jobs.append(lambda a=arm, m=mode, r=rep: run_chain(client, m, a, r))
                for obs_idx in range(len(OTHER_PERSON_OBSERVATIONS)):
                    for rep in range(escalate_reps):
                        jobs.append(
                            lambda a=arm, m=mode, o=obs_idx, r=rep: run_escalate(
                                client, m, a, o, r
                            )
                        )
        with _CLAUSE_LOCK:
            tiers.TYPE_GUIDANCE_PROMPT = original if clause else ""
            try:
                records.extend(_run_jobs(jobs, workers))
            finally:
                tiers.TYPE_GUIDANCE_PROMPT = original
    return records


def main(argv: list[str] | None = None) -> int:
    from tests.evals.harness import build_live_client

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--modes", default="patch,full")
    ap.add_argument("--chain-reps", type=int, default=5)
    ap.add_argument("--escalate-reps", type=int, default=3)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    inner = build_live_client()
    counting = CountingClient(inner)
    records = run_batch(
        counting,
        arms=args.arms.split(","),
        modes=args.modes.split(","),
        chain_reps=args.chain_reps,
        escalate_reps=args.escalate_reps,
        workers=args.workers,
    )
    calls, tin, tout = counting.snapshot()
    rin, rout = _rates_for_model(tiers.DEFAULT_WRITE_MODEL)
    cost = tin / 1e6 * rin + tout / 1e6 * rout
    payload = {
        "model": tiers.DEFAULT_WRITE_MODEL,
        "calls": calls,
        "input_tokens": tin,
        "output_tokens": tout,
        "est_cost_usd": round(cost, 3),
        "summary": summarise(records),
        "records": records,
    }
    args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"calls={calls} in={tin} out={tout} est_cost_usd={cost:.3f}")
    print(json.dumps(payload["summary"], indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

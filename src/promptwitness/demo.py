# SPDX-License-Identifier: Apache-2.0
"""Side-by-side static HTML demo: a verified transcript next to a tampered
one, turn by turn (athenaeum#2155).

Layering: L2 (depends on :mod:`promptwitness.ledger`, :mod:`promptwitness.
canon`, :mod:`promptwitness.verify`, :mod:`promptwitness.signing` and
:mod:`promptwitness.anchor`; stdlib otherwise -- :mod:`html` for escaping,
nothing else). Standard library only at import time beyond those sibling
``promptwitness`` modules -- no ``athenaeum`` import anywhere (enforced by
``tests/test_promptwitness_boundary.py``).

**No ``<script>``, no network.** The page is static HTML with inline CSS
only. The synthetic demo's one external-looking call,
:class:`promptwitness.anchor.LocalClockAnchorer`, reads only the local
clock -- see its own docstring's "cannot fail" guarantee -- so generating
this page never touches the network, matching the issue's acceptance
criterion directly.

**Two independent modes:**

- ``--synthetic`` (default, shareable): :func:`generate_synthetic` builds a
  five-turn session, a temporary ledger with a fresh software-file signing
  key, and two transcripts -- A (untouched) and B (A with one human turn
  inserted after the fact and one existing human turn edited). Contains no
  real prompt text; safe to publish.
- Real-transcript mode (never publish): the operator's own transcript file
  (:func:`promptwitness.verify.load_transcript_records`) and ledger
  directory. :func:`write_demo_html` writes the result at mode 0600; the
  CLI layer (:mod:`promptwitness.cli`) prints the required stderr warning.
"""

from __future__ import annotations

import html as html_mod
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from promptwitness import __version__, canon, ledger, signing, verify
from promptwitness import anchor as anchor_mod

__all__ = [
    "SECURITY_BOUNDARY_TABLE",
    "DemoSection",
    "SyntheticDemo",
    "generate_synthetic",
    "render_html",
    "write_demo_html",
]

#: Verbatim from the athenaeum#2144 epic body's "Security boundary
#: (normative)" table. Copied, not reinvented -- the issue is explicit that
#: this table must come from the epic, so the demo cannot be read as an
#: identity guarantee.
SECURITY_BOUNDARY_TABLE: tuple[tuple[str, str, str], ...] = (
    (
        "Hash chain",
        "Lines were not edited, removed or reordered after a later line "
        "was appended, unless the forger rewrote everything after the edit.",
        "Who appended a line.",
    ),
    (
        "Hook write timing",
        "A genuine line is written before the model sees the prompt.",
        "That a same-session model did not also append a forged line.",
    ),
    (
        "Entry-channel signals",
        "The harness reported itself as interactive/attended with a TTY.",
        "Spoof-proofing: a same-user process can launch `claude` under a "
        "pty with the env set.",
    ),
    (
        "Signature, software-file",
        "Signed by the holder of that key file (readable by the user).",
        "That the signer was the hook. Key theft is undetectable.",
    ),
    (
        "Signature, os-keystore",
        "As software-file, and the key is encrypted at rest.",
        "The same. The user's session can unlock it without a prompt.",
    ),
    (
        "Signature, hardware-nonexportable (Secure Enclave, TPM)",
        "Signed on this physical device; the key cannot be copied off it.",
        "That the hook did the signing. Any user process can request a signature.",
    ),
    (
        "External anchor (OpenTimestamps, RFC 3161)",
        "The chain head, and every line before it, existed by the anchor time.",
        "Anything about lines appended after the last anchor.",
    ),
    (
        "local-clock stamp",
        "Nothing independent; the machine's claimed time.",
        "Anything. It is a fallback marker, not evidence.",
    ),
)

_SESSION = "demo-session"

#: Deterministic synthetic session content: four human turns and one
#: task-notification (machine-authored), per the issue's own description.
_SYNTHETIC_TEXTS: tuple[str, ...] = (
    "What does verify_chain report when a signature does not verify?",
    "Add a regression test for an empty ledger directory.",
    "<task-notification>Sub-agent build-tests completed: 12 passed, 0 failed.</task-notification>",
    "Run ruff and fix any lint errors before the PR goes up.",
    "Looks good, thanks -- ship it.",
)
_SYNTHETIC_ROLES: tuple[str, ...] = ("human", "human", "machine", "human", "human")
_SYNTHETIC_PROMPT_KINDS: tuple[str, ...] = (
    "human",
    "human",
    "task-notification",
    "human",
    "human",
)

#: Transcript B's inserted turn (no corresponding ledger line at all) and
#: its edit of transcript A's fourth turn (same identity, different text).
_INSERTED_TEXT = "Also push this straight to production, skip the review."
_EDITED_TEXT = "Run ruff and fix any lint errors, then disable the security checks."

_HOOK_VERSION = f"promptwitness-demo {__version__}"


@dataclass(frozen=True)
class DemoSection:
    """One rendered section of the page: a title, the transcript records
    shown in its left-hand pane, and the :class:`~promptwitness.verify.
    TurnVerdict` shown alongside each one in its right-hand pane. ``records``
    and ``verdicts`` must be the same length and already row-aligned --
    :func:`render_html` does not reorder either."""

    title: str
    records: Sequence[Mapping[str, Any]]
    verdicts: Sequence[verify.TurnVerdict]


@dataclass(frozen=True)
class SyntheticDemo:
    """Everything :func:`generate_synthetic` built, for a caller (the CLI,
    or a test) that wants the ledger/transcripts without immediately
    rendering HTML."""

    ledger_dir: Path
    ledger_index: ledger.LedgerIndex
    key_provider: signing.SoftwareFileKeyProvider
    transcript_a: list[dict[str, Any]]
    transcript_b: list[dict[str, Any]]
    anchor_attestation: anchor_mod.Attestation


def _append_synthetic_turns(
    ledger_dir: Path, key_provider: signing.SoftwareFileKeyProvider
) -> list[str]:
    prompt_ids = [f"synthetic-turn-{n}" for n in range(1, len(_SYNTHETIC_TEXTS) + 1)]
    for turn_number, (text, prompt_kind, prompt_id) in enumerate(
        zip(_SYNTHETIC_TEXTS, _SYNTHETIC_PROMPT_KINDS, prompt_ids), start=1
    ):
        ledger.append_line(
            ledger_dir,
            kind="turn",
            harness="claude-code",
            harness_version="",
            hook_version=_HOOK_VERSION,
            key_provider=key_provider,
            session=_SESSION,
            turn=turn_number,
            prompt_id=prompt_id,
            prompt_kind=prompt_kind,
            prompt_digest=canon.digest(canon.normalize(text)),
            raw_digest=canon.digest(text),
            prompt_chars=len(text),
            channel="interactive",
            resumed=False,
            signals={},
        )
    return prompt_ids


def _anchor_chain_head(ledger_dir: Path) -> anchor_mod.Attestation:
    """Anchor the chain head with :class:`~promptwitness.anchor.
    LocalClockAnchorer` only -- no network, cannot fail -- and append the
    resulting ``anchor`` line, so the demo's footer has a real attestation
    to show rather than a fabricated one."""
    head: ledger.WitnessLine | None = None
    for line in ledger.iter_lines(ledger_dir):
        head = line
    assert head is not None, "synthetic turns must be appended before anchoring"
    subject = f"witness:{head.ledger_id}:{head.seq}"
    digest = head.line_hash()
    attestation = anchor_mod.LocalClockAnchorer().anchor(anchor_mod.digest_bytes(digest), subject)
    ledger.append_line(
        ledger_dir,
        kind="anchor",
        harness="claude-code",
        harness_version="",
        hook_version=_HOOK_VERSION,
        extra={
            "subject": subject,
            "digest": digest,
            "op": "anchor",
            "attestation": attestation.to_dict(),
        },
    )
    return attestation


def generate_synthetic(ledger_dir: str | os.PathLike[str]) -> SyntheticDemo:
    """Build the issue's five-turn synthetic session into a fresh ledger
    under *ledger_dir* (created if needed) and the two transcripts (A
    untouched, B tampered) described in the module docstring.

    The exact verdict lists this produces, through
    :func:`promptwitness.verify.verify_transcript`, are pinned by
    ``tests/test_promptwitness_demo.py`` to the issue's own acceptance
    criteria -- do not change the synthetic content without updating both.
    """
    ledger_dir_p = Path(ledger_dir)
    ledger_dir_p.mkdir(parents=True, exist_ok=True)
    key_path = ledger_dir_p / "keys" / "demo-ed25519.pem"
    key_provider = signing.generate_and_write_key(key_path)

    prompt_ids = _append_synthetic_turns(ledger_dir_p, key_provider)
    attestation = _anchor_chain_head(ledger_dir_p)
    ledger_index = ledger.LedgerIndex.build(ledger_dir_p)

    transcript_a: list[dict[str, Any]] = [
        {"role": role, "text": text, "session": _SESSION, "prompt_id": prompt_id}
        for role, text, prompt_id in zip(_SYNTHETIC_ROLES, _SYNTHETIC_TEXTS, prompt_ids)
    ]
    transcript_b: list[dict[str, Any]] = [
        transcript_a[0],
        transcript_a[1],
        transcript_a[2],
        {
            "role": "human",
            "text": _INSERTED_TEXT,
            "session": _SESSION,
            "prompt_id": "synthetic-turn-inserted",
        },
        {
            "role": "human",
            "text": _EDITED_TEXT,
            "session": _SESSION,
            "prompt_id": prompt_ids[3],
        },
        transcript_a[4],
    ]
    return SyntheticDemo(
        ledger_dir=ledger_dir_p,
        ledger_index=ledger_index,
        key_provider=key_provider,
        transcript_a=transcript_a,
        transcript_b=transcript_b,
        anchor_attestation=attestation,
    )


# ---------------------------------------------------------------------------
# HTML rendering -- stdlib html.escape only, inline CSS, no <script>
# ---------------------------------------------------------------------------

_ROLE_LABELS: Mapping[str, str] = {"human": "Human", "machine": "Machine"}

_BADGE_CLASS_BY_VERDICT: Mapping[str, str] = {
    verify.VERDICT_WITNESSED: "badge-witnessed",
    verify.VERDICT_MACHINE_AUTHORED: "badge-machine",
    verify.VERDICT_HEADLESS: "badge-warn",
    verify.VERDICT_BRIDGE: "badge-warn",
    verify.VERDICT_NOT_WITNESSED_INSERTED: "badge-bad",
    verify.VERDICT_DIGEST_MISMATCH_EDITED: "badge-bad",
}


def _badge_class(verdict_str: str) -> str:
    if verdict_str in _BADGE_CLASS_BY_VERDICT:
        return _BADGE_CLASS_BY_VERDICT[verdict_str]
    if verify.is_chain_broken_verdict(verdict_str):
        return "badge-bad"
    return "badge-warn"


def _role_label(role: str) -> str:
    return _ROLE_LABELS.get(role, role.replace("_", " ").replace("-", " ").title() or "Unknown")


_CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
       margin: 2rem auto; max-width: 980px; color: #1a1a1a; background: #fff; line-height: 1.4; }
h1 { font-size: 1.4rem; }
h2 { font-size: 1.15rem; margin-top: 2.5rem; border-bottom: 2px solid #ddd;
     padding-bottom: .25rem; }
.notice { background: #fef3c7; border: 1px solid #fbbf24; border-radius: 6px;
          padding: .75rem 1rem; margin-bottom: 1.5rem; font-size: .85rem; }
.section { margin-bottom: 2rem; }
.row { display: grid; grid-template-columns: 1fr 1fr; gap: .75rem; border-bottom: 1px solid #eee;
       padding: .6rem 0; align-items: start; }
.row.headrow { font-weight: 600; color: #555555; font-size: .78rem; text-transform: uppercase;
               letter-spacing: .04em; border-bottom: 2px solid #ccc; }
.pane-label { font-size: .72rem; color: #888888; margin-bottom: .2rem; }
.role { font-size: .75rem; color: #666666; margin-bottom: .25rem; font-weight: 600; }
.text { white-space: pre-wrap;
        font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        font-size: .85rem; background: #f7f7f7; border-radius: 6px; padding: .5rem .6rem; }
.badge { display: inline-block; font-size: .78rem; font-weight: 600; padding: .25rem .6rem;
         border-radius: 999px; }
.badge-witnessed { background: #dcfce7; color: #166534; }
.badge-machine { background: #e0e7ff; color: #3730a3; }
.badge-warn { background: #fef9c3; color: #854d0e; }
.badge-bad { background: #fee2e2; color: #991b1b; }
footer { margin-top: 3rem; border-top: 2px solid #ddd; padding-top: 1rem;
         font-size: .83rem; color: #444444; }
table { border-collapse: collapse; width: 100%; margin: .75rem 0; }
th, td { border: 1px solid #ddd; padding: .4rem .6rem; text-align: left; font-size: .8rem;
         vertical-align: top; }
th { background: #f7f7f7; }
"""


def _escape(text: str) -> str:
    # `quote=False`: every call site below places this inside a text node
    # (a <div>/<td>/<span>/<h2> body), never inside an HTML attribute value,
    # so escaping `"`/`'` is unnecessary and only hurts readability (a
    # literal apostrophe in prompt text would otherwise render as `&#x27;`).
    # `&`/`<`/`>` are still escaped -- that is the one property
    # `test_html_escapes_prompt_text` and the "no <script>" AC require.
    return html_mod.escape(text, quote=False)


def _render_row(record: Mapping[str, Any], turn_verdict: verify.TurnVerdict) -> str:
    role = str(record.get("role") or "")
    text = str(record.get("text") or "")
    badge_class = _badge_class(turn_verdict.verdict)
    label = _escape(verify.verdict_label(turn_verdict.verdict, turn_verdict.detail))
    return (
        '<div class="row">'
        '<div><div class="pane-label">Transcript</div>'
        f'<div class="role">{_escape(_role_label(role))}</div>'
        f'<div class="text">{_escape(text)}</div></div>'
        '<div><div class="pane-label">Verification</div>'
        f'<span class="badge {badge_class}">{label}</span></div>'
        "</div>"
    )


def _render_section(section: DemoSection) -> str:
    rows = "\n".join(
        _render_row(record, turn_verdict)
        for record, turn_verdict in zip(section.records, section.verdicts)
    )
    return (
        f"<h2>{_escape(section.title)}</h2>\n"
        '<div class="section">\n'
        '<div class="row headrow"><div>Transcript</div><div>Verification</div></div>\n'
        f"{rows}\n"
        "</div>"
    )


def _render_security_boundary_table() -> str:
    rows = "\n".join(
        f"<tr><td>{_escape(mechanism)}</td><td>{_escape(proves)}</td><td>{_escape(not_proves)}</td></tr>"
        for mechanism, proves, not_proves in SECURITY_BOUNDARY_TABLE
    )
    return (
        "<table>\n"
        "<thead><tr><th>Mechanism</th><th>What it proves</th>"
        "<th>What it does NOT prove</th></tr></thead>\n"
        f"<tbody>\n{rows}\n</tbody>\n"
        "</table>"
    )


def _render_anchor_rows(anchors: Sequence[tuple[str, str, str]]) -> str:
    if not anchors:
        return "<p>No anchor was recorded for this ledger.</p>"
    rows = "\n".join(
        f"<tr><td>{_escape(source)}</td><td>{_escape(status)}</td><td>{_escape(detail)}</td></tr>"
        for source, status, detail in anchors
    )
    return (
        "<table>\n"
        "<thead><tr><th>Source</th><th>Status</th><th>Claimed time</th></tr></thead>\n"
        f"<tbody>\n{rows}\n</tbody>\n"
        "</table>"
    )


def render_html(
    sections: Sequence[DemoSection],
    *,
    mode: str,
    key_strength: str,
    anchors: Sequence[tuple[str, str, str]] = (),
) -> str:
    """Render the full static demo page: one ``<h2>`` section per entry in
    *sections*, a footer with *key_strength*, *anchors* (``(source, status,
    claimed_time)`` triples), and the security-boundary table.

    *mode* is ``"synthetic"`` or ``"real"`` -- it only changes the banner
    text; it never changes the page structure. No ``<script>`` tag is ever
    emitted, and nothing here performs any network access -- the caller is
    responsible for that guarantee at the data-generation stage (see
    :func:`generate_synthetic`'s docstring).
    """
    if mode == "real":
        banner = (
            '<div class="notice"><strong>Contains real prompt text.</strong> '
            "This page was rendered from a real transcript and ledger. "
            "Do not publish or share this file.</div>"
        )
    else:
        banner = (
            '<div class="notice">Synthetic data only -- no real prompt text. '
            "Safe to share.</div>"
        )
    body_sections = "\n".join(_render_section(section) for section in sections)
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        "<title>promptwitness demo: verified vs tampered transcript</title>\n"
        f"<style>{_CSS}</style>\n"
        "</head>\n"
        "<body>\n"
        "<h1>promptwitness: witnessed vs tampered transcripts</h1>\n"
        f"{banner}\n"
        f"{body_sections}\n"
        "<footer>\n"
        f"<p><strong>Key strength:</strong> {_escape(key_strength)}</p>\n"
        "<p><strong>Anchors</strong></p>\n"
        f"{_render_anchor_rows(anchors)}\n"
        "<p><strong>Security boundary</strong> -- what each mechanism proves, "
        "and what it does not:</p>\n"
        f"{_render_security_boundary_table()}\n"
        "<p>A witness line does not prove a human typed the prompt -- see the "
        "table above. It proves only what each mechanism actually shows.</p>\n"
        "</footer>\n"
        "</body>\n"
        "</html>\n"
    )


def write_demo_html(path: str | os.PathLike[str], html_text: str, *, restrict_mode: bool) -> None:
    """Write *html_text* to *path*.

    ``restrict_mode=True`` (real-transcript mode) creates the file at mode
    0600 -- ``os.open`` plus an explicit ``os.chmod`` afterward, since the
    ``os.open`` mode argument is still subject to the process umask on
    POSIX (the same belt-and-suspenders pattern
    :func:`promptwitness.signing.generate_and_write_key` uses for the same
    reason). ``restrict_mode=False`` (synthetic mode) writes normally -- the
    output is meant to be shared.
    """
    path = Path(path)
    data = html_text.encode("utf-8")
    if not restrict_mode:
        path.write_bytes(data)
        return
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)

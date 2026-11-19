"""Guard: no fixed eight-hex page-uid pattern in ``src/athenaeum`` (athenaeum#2771).

New pages carry a 16-hex uid; pages minted earlier keep their 8-hex uid. A regex
that matches *exactly* eight hex characters would refuse or mis-parse the new
width, so any such literal must be on the allowlist below with the reason it is
not a page uid.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "athenaeum"

# A hex character class followed by an exact ``{8}`` quantifier.
_EIGHT_HEX = re.compile(
    r"\[(?:0-9a-f|a-f0-9|0-9a-fA-F|a-fA-F0-9|0-9A-Fa-f|A-Fa-f0-9|0-9A-F|A-F0-9)\]\{8\}"
)

_SIXTEEN_HEX = re.compile(_EIGHT_HEX.pattern.replace(r"\{8\}", r"\{16\}"))

# (file relative to src/athenaeum, substring identifying the site) -> reason.
ALLOWLIST: dict[tuple[str, str], str] = {
    ("intake.py", "RAW_FILE_RE"): (
        "raw-intake filename suffix (uuid8 field), not a page uid"
    ),
}


def find_violations(files: dict[str, str]) -> list[str]:
    """Return ``file:line`` for each exact-8-hex literal not on the allowlist."""
    out: list[str] = []
    for rel, text in files.items():
        for lineno, line in enumerate(text.splitlines(), 1):
            if not _EIGHT_HEX.search(line):
                continue
            if _SIXTEEN_HEX.search(line):
                continue  # accepts both widths (8 or 16), which is the goal
            if any(rel == f and marker in line for (f, marker) in ALLOWLIST):
                continue
            out.append(f"{rel}:{lineno}: {line.strip()}")
    return out


def _src_files() -> dict[str, str]:
    return {
        str(p.relative_to(SRC)): p.read_text(encoding="utf-8")
        for p in sorted(SRC.rglob("*.py"))
    }


def test_no_fixed_eight_hex_uid_pattern_in_src() -> None:
    assert find_violations(_src_files()) == []


def test_allowlist_entries_are_still_live() -> None:
    files = _src_files()
    for (rel, marker), reason in ALLOWLIST.items():
        assert reason
        assert any(
            marker in line and _EIGHT_HEX.search(line)
            for line in files[rel].splitlines()
        ), f"stale allowlist entry {rel}:{marker}"


def test_guard_fails_on_a_synthetic_new_eight_hex_uid_pattern() -> None:
    synthetic = {"newmod.py": 'UID_RE = re.compile(r"^[0-9a-f]{8}-")\n'}
    assert find_violations(synthetic) == [
        'newmod.py:1: UID_RE = re.compile(r"^[0-9a-f]{8}-")'
    ]
    both = {"ok.py": 'UID_RE = re.compile(r"^([0-9a-f]{16}|[0-9a-f]{8})-")\n'}
    assert find_violations(both) == []
    # The allowlisted raw-intake site does not trip it.
    allowed = {
        "intake.py": 'RAW_FILE_RE = re.compile(r"^(\\d{8}T)-([0-9a-f]{8})\\.md$")\n'
    }
    assert find_violations(allowed) == []

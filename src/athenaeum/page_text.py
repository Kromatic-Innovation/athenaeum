# SPDX-License-Identifier: Apache-2.0
"""Section and list helpers shared by the page-writing merges (athenaeum#2773).

Pure text helpers with no athenaeum imports: a CommonMark-aware H2 splitter,
code-fence tracking, line normalisation and list coercion. They were private
to :mod:`athenaeum.structured_upsert` (a page merged with a raw); the
three-way :mod:`athenaeum.page_merge` needs the very same definition of a
section, so both modules read it from here and a section means one thing.

Layering: L0, stdlib only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

H2_RE = re.compile(r"^##[ \t]+(.+?)[ \t]*#*[ \t]*$")
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
BULLET_RE = re.compile(r"^[-*+][ \t]+")


@dataclass
class Section:
    title: str
    heading: str
    text: str

    @property
    def key(self) -> str:
        return self.title.casefold()


def advance_fence(line: str, state: tuple[str, int] | None) -> tuple[str, int] | None:
    """CommonMark fence tracking: the state after *line*.

    A fence opens on three or more backticks or tildes (a backtick opener's info
    string may not contain a backtick) and closes only on the same character, at
    least as long as the opener, with nothing after it but whitespace.
    """
    m = FENCE_RE.match(line.rstrip("\n"))
    if m is None:
        return state
    run, rest = m.group(1), m.group(2)
    if state is None:
        if run[0] == "`" and "`" in rest:
            return None
        return (run[0], len(run))
    if run[0] == state[0] and len(run) >= state[1] and not rest.strip():
        return None
    return state


def ends_in_open_fence(body: str) -> bool:
    state: tuple[str, int] | None = None
    for line in body.replace("\r\n", "\n").splitlines():
        state = advance_fence(line, state)
    return state is not None


def split_sections(body: str) -> tuple[str, list[Section]]:
    """Split *body* into (preamble, H2 sections), ignoring ``##`` inside fences."""
    preamble: list[str] = []
    sections: list[Section] = []
    fence: tuple[str, int] | None = None
    cur: Section | None = None
    for line in body.replace("\r\n", "\n").splitlines(keepends=True):
        match = None if fence else H2_RE.match(line.rstrip("\n"))
        fence = advance_fence(line, fence)
        if match:
            cur = Section(title=match.group(1).strip(), heading=line, text="")
            sections.append(cur)
        elif cur is None:
            preamble.append(line)
        else:
            cur.text += line
    return "".join(preamble), sections


def norm_line(line: str) -> str:
    return BULLET_RE.sub("", line.strip()).casefold()


def as_list(value: object) -> list[object]:
    if value is None or value == "":
        return []
    return list(value) if isinstance(value, list) else [value]

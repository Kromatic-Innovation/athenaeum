# SPDX-License-Identifier: Apache-2.0
"""Proof pair for the ``reads_live_knowledge_tree`` declared opt-in
(athenaeum#2183 follow-up, Seer finding on PR athenaeum#2264).

``tests/fixtures/default_root_guard.py``'s autouse ``sys.addaudithook``
forbids any test from opening a path under the real default knowledge root
or default cache dir -- except a test that declares
``@pytest.mark.reads_live_knowledge_tree`` (exactly three tests in
``tests/test_eval_corpus_leakage.py`` carry it, because their documented
purpose is reading the real tree). A fix that only adds the opt-in, with no
test proving the guard still fires for everyone else, is a WEAKENED guard
wearing a narrowed one's name. These two tests are that proof: one shows
the marker lifts the guard, the other shows an undeclared attempt still
raises. Both call the audit-hook callback directly, rather than performing
a real filesystem open, so neither test depends on -- or touches -- the
real default knowledge root actually existing on the machine running the
suite.
"""

from __future__ import annotations

import pytest

from tests.fixtures import default_root_guard

#: A representative forbidden path -- the real default knowledge root with
#: a filename appended. Never actually opened by either test below.
_FORBIDDEN_PATH = str(default_root_guard.REAL_DEFAULT_KNOWLEDGE_ROOT / "canary.md")


def test_undeclared_open_under_the_real_default_root_still_raises() -> None:
    """AC2's guard fires for an open with no declared opt-in."""
    with pytest.raises(AssertionError, match="athenaeum#2183 guard"):
        default_root_guard._fail_on_forbidden_open("open", (_FORBIDDEN_PATH, "r", 0))


@pytest.mark.reads_live_knowledge_tree
def test_declared_open_under_the_real_default_root_is_permitted() -> None:
    """The identical open the test above forbids is let through for a test
    carrying the declared ``reads_live_knowledge_tree`` marker."""
    default_root_guard._fail_on_forbidden_open("open", (_FORBIDDEN_PATH, "r", 0))  # must not raise

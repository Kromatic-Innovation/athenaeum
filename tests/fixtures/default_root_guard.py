# SPDX-License-Identifier: Apache-2.0
"""Session-wide tripwire: no test may open a path under the REAL default
knowledge root or default cache dir (athenaeum#2183).

Both ``athenaeum.config.DEFAULT_KNOWLEDGE_ROOT`` (the unexpanded
``~/knowledge`` template, re-expanded at every call site) and
``athenaeum.librarian.DEFAULT_KNOWLEDGE_ROOT`` (the same template, but
expanded exactly ONCE, at import time -- before any fixture or test gets a
chance to redirect it) can resolve to the operator's live deployment the
moment a caller omits an explicit override. A CLI test that forgets
``--knowledge-root``/``--path`` -- or a library call that forgets its
``knowledge_root``/``cache_dir`` argument -- then takes the live run lock,
or reads/writes the live corpus. ``tests/test_repair.py``'s bounce-fold
tests hit exactly this: they resolved ``DEFAULT_KNOWLEDGE_ROOT`` and took
the run lock there, so they failed with exit 75 whenever a real librarian
run held that lock -- flaky in a way that depends on the HOST's live
state, not on anything the test itself controls.

Mechanism: install a ``sys.addaudithook`` on the ``"open"`` event, which is
raised uniformly by ``open()``, ``io.open()``, ``os.open()``, and therefore
``pathlib.Path.open()`` -- regardless of which of the ~50 ``_cmd_*.py``
modules' own imported ``DEFAULT_KNOWLEDGE_ROOT`` binding triggered the
call. A guard keyed on the RESOLVED PATH an open actually touches, rather
than on patching every module's bound name, needs no maintenance as new
``_cmd_*`` modules are added, and catches the frozen-at-import
``librarian.DEFAULT_KNOWLEDGE_ROOT`` case that no amount of redirecting
``HOME`` after import could ever reach. Any open under either real default
root raises immediately, failing the offending test loudly instead of
silently touching the live deployment.

The two forbidden roots are captured ONCE, at import time (i.e. before any
fixture or test gets a chance to monkeypatch ``HOME`` / the specific
``_cmd_*`` module's bound ``DEFAULT_KNOWLEDGE_ROOT`` / ``ATHENAEUM_CACHE_DIR``),
via ``Path.home()`` / ``os.path.expanduser`` -- never as a literal path, so
public-safe-lint has nothing to flag -- so the comparison target is fixed
for the whole process and cannot itself be redirected by whatever a given
test does to its own environment.

Registered from ``tests/conftest.py`` via a single ``pytest_plugins`` entry
appended at that module's end, rather than inlined there, per this issue's
lane-brief anchor-discipline note: a sibling lane (athenaeum#2256) is
concurrently editing ``conftest.py`` for unrelated environment-variable
hermeticity work, and a new module keeps both diffs trivially mergeable.

**Declared opt-in (athenaeum#2183 follow-up, Seer finding on PR athenaeum#2264).**
``tests/test_eval_corpus_leakage.py`` has three tests whose entire purpose
is reading the real knowledge tree -- its denylist-of-real-proper-nouns
check needs real frontmatter to compare the eval corpus against, and skips
loudly (never passes silently) when no local tree is present. That read is
not an accident the guard above should catch; it is the test's documented
job. Blanket-exempting an env var would reopen exactly the "missed
override fails loudly" hole AC 2 closes, so the opt-in is instead the
``reads_live_knowledge_tree`` pytest marker (registered in
``pyproject.toml``'s ``markers``) -- declared, per-test, visible at the
test's own definition, and checked by the autouse
``_reads_live_knowledge_tree_opt_in`` fixture below. Every OTHER test is
unaffected: an undeclared open under either forbidden root still raises.
"""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

# Derived via Path.home()/os.path.expanduser, never a literal -- and
# captured at IMPORT time, which is as early as a pytest plugin module can
# run: before collection, before any fixture, before any test has a chance
# to monkeypatch HOME or a module's bound DEFAULT_KNOWLEDGE_ROOT. That
# ordering is what makes this comparison target trustworthy as "the real
# thing", independent of what any later test does to its own environment.
REAL_DEFAULT_KNOWLEDGE_ROOT = Path(os.path.expanduser("~/knowledge"))
REAL_DEFAULT_CACHE_DIR = Path(os.path.expanduser("~/.cache/athenaeum"))

_FORBIDDEN_ROOTS = (REAL_DEFAULT_KNOWLEDGE_ROOT, REAL_DEFAULT_CACHE_DIR)

#: Marker name for the declared opt-in (registered in ``pyproject.toml``'s
#: ``markers`` list). A test carrying ``@pytest.mark.reads_live_knowledge_tree``
#: declares, at its own definition, that it deliberately reads the real
#: default knowledge root or cache dir -- see the module docstring's
#: "Declared opt-in" section. Not an env var: an env var is a blanket
#: switch nothing names at the call site, which is exactly the "missed
#: override" shape AC 2 forbids.
READS_LIVE_KNOWLEDGE_TREE_MARKER = "reads_live_knowledge_tree"

# Depth counter, not a bool: an opted-in test's own fixtures could in
# principle nest (a parametrized fixture re-entering another marked
# context), and a bare bool would let an inner teardown turn the guard back
# on while the outer test body is still running. Mutated only by
# `_reads_live_knowledge_tree_opt_in` below, which pytest runs on the main
# thread for one test at a time -- no lock needed (this suite does not run
# under xdist; see tests/conftest.py's CI-shard comment, which shards by
# process, not by in-process worker thread).
_opt_in_depth = 0


def _opt_in_active() -> bool:
    return _opt_in_depth > 0


@pytest.fixture(autouse=True)
def _reads_live_knowledge_tree_opt_in(request: pytest.FixtureRequest) -> Iterator[None]:
    """Autouse: lifts the guard for exactly the duration of a test carrying
    ``@pytest.mark.reads_live_knowledge_tree`` (athenaeum#2183 follow-up).

    Runs for every test (autouse), but only ever MUTATES the shared opt-in
    state for a marked one -- an unmarked test's open()s are still
    evaluated by :func:`_fail_on_forbidden_open` exactly as before.
    """
    global _opt_in_depth
    marked = request.node.get_closest_marker(READS_LIVE_KNOWLEDGE_TREE_MARKER) is not None
    if marked:
        _opt_in_depth += 1
    try:
        yield
    finally:
        if marked:
            _opt_in_depth -= 1


def _is_under(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _fail_on_forbidden_open(event: str, args: tuple[Any, ...]) -> None:
    """``sys.addaudithook`` callback: raise if *event* opens a path under
    either real default root -- UNLESS the currently-running test declared
    the ``reads_live_knowledge_tree`` opt-in (see
    :func:`_reads_live_knowledge_tree_opt_in`).

    ``args[0]`` is the path for the ``"open"`` audit event; it may be a
    ``str``, ``bytes``, ``os.PathLike``, or (when the caller already holds
    an open file descriptor) an ``int`` -- the ``int`` case cannot be a NEW
    open under a forbidden root, so it is ignored rather than coerced.
    """
    if event != "open":
        return
    if _opt_in_active():
        return
    raw_path = args[0]
    if not isinstance(raw_path, (str, bytes, os.PathLike)):
        return
    candidate = Path(os.fsdecode(raw_path))
    if not candidate.is_absolute():
        return  # a relative path cannot be under either absolute forbidden root
    for root in _FORBIDDEN_ROOTS:
        if _is_under(candidate, root):
            raise AssertionError(
                "athenaeum#2183 guard (tests/fixtures/default_root_guard.py): "
                f"a test opened a path under the REAL default ({root}) "
                f"instead of a tmp override -- attempted path: {candidate}. "
                "Pass an explicit --knowledge-root/--cache-dir (or "
                "knowledge_root=/cache_dir=) tmp override, or redirect the "
                "specific module's bound DEFAULT_KNOWLEDGE_ROOT."
            )


_hook_installed = False


def _install_hook_once() -> None:
    global _hook_installed
    if _hook_installed:
        return
    sys.addaudithook(_fail_on_forbidden_open)
    _hook_installed = True


@pytest.fixture(scope="session", autouse=True)
def _fail_loudly_on_real_default_root_or_cache_dir_open() -> None:
    """Autouse, session-wide (athenaeum#2183 AC2): fail any test that opens
    a path under the real default knowledge root or default cache dir.

    ``sys.addaudithook`` has no removal API -- installing it once and
    leaving it installed for the rest of the process is the intended usage,
    so there is nothing to tear down here.
    """
    _install_hook_once()


@pytest.fixture
def default_knowledge_root_under_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., Path]]:
    """Factory fixture (athenaeum#2183 AC1): redirect one or more modules'
    bound ``DEFAULT_KNOWLEDGE_ROOT`` to a tmp dir and hold a real
    :class:`~athenaeum.runlock.RunLock` there for the rest of the test.

    Reproduces "a real librarian run holds the default root's run lock"
    without ever touching the operator's actual default knowledge root: the
    redirect happens before the lock is taken, so every path this fixture
    creates -- the fake default root, and the ``.athenaeum.lock`` inside it
    -- lives entirely under ``tmp_path``.

    Usage::

        def test_foo(default_knowledge_root_under_lock):
            fake_root = default_knowledge_root_under_lock("athenaeum._cmd_repair")
            # fake_root's run lock is held for the rest of this test; call
            # cli.main(...) with an EXPLICIT --knowledge-root pointing
            # elsewhere to prove the command under test is independent of
            # whatever the (redirected) default resolves to.

    Takes the dotted module name(s) whose locally-imported
    ``DEFAULT_KNOWLEDGE_ROOT`` binding needs redirecting -- each ``_cmd_*.py``
    module does its own ``from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT``,
    so patching ``athenaeum.config`` alone would not reach a module that
    already imported the name into its own namespace. Same pattern
    ``tests/test_push_metrics_cli.py`` already uses for a single module
    (``athenaeum._cmd_push_metrics.DEFAULT_KNOWLEDGE_ROOT``); this
    generalizes it to N modules sharing one held lock.
    """
    from athenaeum.runlock import RunLock

    fake_root = tmp_path / "fake-default-knowledge-root"
    fake_root.mkdir()
    lock = RunLock(fake_root, wait=0)
    lock.acquire()

    def _redirect(*module_names: str) -> Path:
        for dotted in module_names:
            module = importlib.import_module(dotted)
            monkeypatch.setattr(module, "DEFAULT_KNOWLEDGE_ROOT", fake_root)
        return fake_root

    try:
        yield _redirect
    finally:
        lock.release()

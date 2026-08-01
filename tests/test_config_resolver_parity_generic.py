# SPDX-License-Identifier: Apache-2.0
"""Issue #555 (M27) — a GENERIC parity test over every ``config.resolve_*``.

``tests/test_config_parity.py`` mirrors the resolver surface BY HAND: it
hand-picks a handful of knobs and asserts their precedence. That means a new
or CHANGED resolver that silently stops reading its documented yaml key (or
env var) slips through unless someone remembers to add a matching hand test.
This is not hypothetical — commit 4324f04 (#512/#513) fixed exactly this:
the contradiction-resolver model's ``_get_model`` (in ``resolutions.py``) had
its own hand-rolled env/yaml chain that never consulted ``models.resolve``,
and the hand-written parity suite did not catch it because nobody had
written a case for it.

This module instead ENUMERATES every ``resolve_*`` function on
``athenaeum.config`` via ``inspect.getmembers`` — no hand-maintained list —
and, for each one, uses the resolver's OWN SOURCE CODE to discover which
yaml key(s) and env var(s) it references, then verifies BY CONSTRUCTION that
setting those values actually changes the resolver's return value. A
resolver added tomorrow is automatically pulled into this test with zero
edits here.

Strategy (see ``_literal_probe`` / ``_env_and_yaml_literals``):

1. Enumerate ``resolve_*`` functions defined in ``athenaeum.config`` via
   ``inspect.getmembers(config, inspect.isfunction)``.
2. For each resolver, use ``inspect.getsource`` + ``ast`` to collect every
   string-literal argument passed to any function call in its body (this
   covers both a direct ``some_dict.get("key")`` call AND a resolver that
   delegates to a shared private helper like ``_resolve_positive_int_knob(
   config, "page_warn_bytes", "ATHENAEUM_PAGE_WARN_BYTES", 8192)`` — the key
   and env var show up as call-site literal arguments either way). Literals
   matching ``ATHENAEUM_[A-Z0-9_]+`` are candidate env vars; other
   lowercase-identifier-shaped literals are candidate yaml keys.
3. Because resolvers nest yaml keys to different depths (``librarian.max_
   merge_sources`` vs. the two-deep ``librarian.delta.enabled`` vs. the
   three-deep ``owner.asserter`` under ``owner``), and the literal-discovery
   order doesn't reliably reflect nesting order (a delegated helper's own
   literals are appended after the call site's), we don't guess ONE nesting
   and hand-assert it. Instead we try EVERY plausible nesting (every
   contiguous suffix of the discovered key list, forward and reversed) as a
   candidate config dict with a unique sentinel at the leaf, call the
   resolver, and require that AT LEAST ONE candidate changes the resolver's
   output relative to the ``config=None`` baseline. This sidesteps the
   ordering ambiguity while still proving the resolver actually reads
   *something* it claims to read — a resolver that silently stopped reading
   its key would make EVERY candidate a no-op, exactly the #512 failure
   shape.
4. Independently, for each discovered ``ATHENAEUM_*`` literal, set that env
   var to a sentinel string (via ``monkeypatch``) and require the resolver's
   output differs from the no-env baseline (again trying config=None and a
   couple of representative configs, since some resolvers only consult the
   env var and ignore config entirely).

Exclusions (kept minimal, each justified, and asserted-closed so a new
resolver cannot silently join the exclusion set without this file changing):

- ``resolve_cache_dir``: takes NO ``config`` parameter at all (signature is
  ``resolve_cache_dir(cache_dir: Path | None = None)``); it resolves the
  cache directory from an explicit ``cache_dir`` arg, then
  ``ATHENAEUM_CACHE_DIR``, then a code default. It has an env var (verified
  below) but genuinely no yaml key — excluded from the yaml-key check only.
- ``resolve_model``: a generic parameterized helper
  (``resolve_model(knob, env_var, default, config)``) — the yaml key and env
  var are CALLER-supplied arguments, not literals baked into this function's
  body, so the source-introspection strategy cannot discover them from
  ``resolve_model`` itself. It is instead exercised directly with explicit
  arguments (still generically, not hand-listing its callers).

Every other resolver goes through the full generic check.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path
from typing import Any

import pytest

from athenaeum import config as config_mod

# ---------------------------------------------------------------------------
# Enumeration (NOT a hand-maintained list): every `resolve_*` function that
# is actually DEFINED in athenaeum.config (excludes re-exports, if any).
# ---------------------------------------------------------------------------

_ALL_RESOLVERS: list[tuple[str, Any]] = [
    (name, fn)
    for name, fn in inspect.getmembers(config_mod, inspect.isfunction)
    if name.startswith("resolve_") and fn.__module__ == config_mod.__name__
]

_RESOLVER_NAMES = sorted(name for name, _ in _ALL_RESOLVERS)

# The small, justified exclusion set (see module docstring). Membership is
# asserted below so a new resolver cannot land in it silently.
_NO_YAML_KEY = frozenset({"resolve_cache_dir"})
_GENERIC_HELPER_SIGNATURE = frozenset({"resolve_model"})

_ENV_VAR_RE = re.compile(r"^ATHENAEUM_[A-Z0-9_]+$")
_YAML_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _call_site_string_literals(source: str) -> list[str]:
    """Every string-literal positional/keyword arg passed to any Call in
    *source*, in AST visit order. Covers ``d.get("key")``,
    ``os.environ.get("ATHENAEUM_X")``, and delegation to a private helper
    like ``_resolve_positive_int_knob(config, "key", "ATHENAEUM_X", default)``
    uniformly, since in every case the key/env-var shows up as a literal
    call argument somewhere in the function body.
    """
    tree = ast.parse(source)
    literals: list[str] = []

    class _Visitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    literals.append(arg.value)
            for kw in node.keywords:
                if isinstance(kw.value, ast.Constant) and isinstance(
                    kw.value.value, str
                ):
                    literals.append(kw.value.value)
            self.generic_visit(node)

    _Visitor().visit(tree)
    return literals


def _called_private_helpers(source: str) -> list[str]:
    """Names of any ``_resolve*``-prefixed helper functions called in *source*."""
    tree = ast.parse(source)
    names: list[str] = []

    class _Visitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            if isinstance(node.func, ast.Name) and node.func.id.startswith(
                "_resolve"
            ):
                if node.func.id not in names:
                    names.append(node.func.id)
            self.generic_visit(node)

    _Visitor().visit(tree)
    return names


def _env_and_yaml_literals(fn: Any) -> tuple[list[str], list[str]]:
    """Return (env_var_candidates, yaml_key_candidates) for resolver *fn*.

    Follows one level of delegation into any private ``_resolve*`` helper
    the resolver calls (e.g. ``_resolve_positive_int_knob``,
    ``_resolve_glob_list``, ``_resolve_sample_rate``,
    ``_resolve_optional_positive_number``) so a key/env-var that only
    appears in the shared helper's OWN body (like ``_resolve_glob_list``'s
    ``config.get("recall")``) is still discovered.
    """
    source = inspect.getsource(fn)
    literals = list(_call_site_string_literals(source))
    for helper_name in _called_private_helpers(source):
        helper = getattr(config_mod, helper_name, None)
        if helper is not None:
            literals += _call_site_string_literals(inspect.getsource(helper))

    env_vars = list(dict.fromkeys(s for s in literals if _ENV_VAR_RE.match(s)))
    yaml_keys = list(
        dict.fromkeys(s for s in literals if _YAML_KEY_RE.match(s) and s not in env_vars)
    )
    return env_vars, yaml_keys


# Resolvers coerce their yaml value to their own knob type (float/int/bool/
# str/list) and silently fall back to default on a type mismatch (by policy
# -- see config.py's malformed-value docstring) rather than raising. A
# single string sentinel therefore looks IDENTICAL to "not set" for a
# numeric/bool/list-typed knob. Rather than hand-map each resolver to its
# return type, try a small battery of differently-typed sentinel VALUES —
# whichever type the real knob is, at least one candidate will be well-typed
# and therefore observable if the resolver actually reads it.
_SENTINEL_VALUES: tuple[Any, ...] = (
    "__parity_sentinel_str__",
    0.987654321,
    123456789,
    True,
    False,
    ["__parity_sentinel_item__"],
    {"__parity_sentinel_key__": "__parity_sentinel_value__"},
    {"__parity_sentinel_key__": {"__parity_sentinel_nested__": "x"}},
)


def _candidate_nestings(yaml_keys: list[str], sentinel_tag: str) -> list[dict]:
    """Every plausible nested-dict shape built from *yaml_keys*, crossed with
    a battery of differently-typed sentinel leaf values.

    We don't know the true nesting order a priori (a delegated helper's
    literals are appended after the call site's, so e.g. ``resolve_index_
    globs`` discovers ``["include_globs", "recall"]`` — key before section).
    So we try every contiguous suffix of the discovered key list, in both
    the discovered order and reversed, nesting each as
    ``{k0: {k1: {...: sentinel}}}`` for EACH candidate sentinel value/type
    (see ``_SENTINEL_VALUES``). We additionally try a SIBLING shape for
    resolvers with >= 3 discovered keys (e.g. ``resolve_screening`` reads
    ``screening.medical.action`` AND the sibling ``screening.medical.access``
    -- ``action``/``access`` are two independent leaves under one shared
    parent, not a linear chain): every prefix length is tried with the
    remaining keys as SIBLING leaves under that prefix, all set to the same
    sentinel simultaneously. Only one combination needs to actually match the
    resolver's real structure AND type for the parity check to pass; if the
    resolver stopped reading ALL of its documented keys, every candidate is a
    no-op and the test correctly fails.
    """
    candidates: list[dict] = []
    seen: set[tuple[str, ...]] = set()
    n = len(yaml_keys)

    def _nest(path: list[str], leaf: Any) -> dict:
        built = leaf
        for seg in reversed(path):
            built = {seg: built}
        return built

    for k in range(1, n + 1):
        for path in (yaml_keys[-k:], list(reversed(yaml_keys[-k:]))):
            key = tuple(path)
            if key in seen:
                continue
            seen.add(key)
            for sentinel in _SENTINEL_VALUES:
                candidates.append(_nest(path, sentinel))

    # Sibling shape: split the (forward-order) key list into a prefix and a
    # set of sibling leaves under that prefix.
    for prefix_len in range(0, n - 1):
        prefix = yaml_keys[:prefix_len]
        siblings = yaml_keys[prefix_len:]
        if len(siblings) < 2:
            continue
        for sentinel in _SENTINEL_VALUES:
            leaf = {s: sentinel for s in siblings}
            key = ("SIBLING", tuple(prefix), tuple(siblings), repr(sentinel))
            if key in seen:
                continue
            seen.add(key)
            candidates.append(_nest(prefix, leaf))

    return candidates


# Resolvers whose first parameter is NOT `config` need a bit of generic
# calling-convention help (not an expectations list — just how to invoke
# them positionally). Discovered generically from the signature itself.
def _invoke(fn: Any, config: Any, knowledge_root: Path) -> Any:
    params = list(inspect.signature(fn).parameters.keys())
    if params and params[0] == "knowledge_root":
        return fn(knowledge_root, config)
    # default: config-first (possibly with trailing optional args, which we
    # omit — every resolver in this shape accepts a lone `config` call).
    return fn(config)


# ---------------------------------------------------------------------------
# The enumeration itself must be non-trivial and non-hand-maintained: guard
# that we actually found the expected order of magnitude of resolvers, and
# that the exclusion sets are both subsets of what was actually enumerated
# (so a typo'd exclusion doesn't silently exclude nothing / everything).
# ---------------------------------------------------------------------------


def test_enumeration_found_the_resolver_surface() -> None:
    """Sanity floor: config.py is documented as ~50 resolvers; if enumeration
    finds far fewer, `inspect.getmembers` is broken or the module shrank
    drastically — either way this generic test would be silently checking
    nothing."""
    assert len(_ALL_RESOLVERS) >= 40, (
        f"only found {len(_ALL_RESOLVERS)} resolve_* functions in "
        "athenaeum.config — expected ~50; enumeration may be broken"
    )


def test_exclusion_sets_are_subsets_of_enumerated_resolvers() -> None:
    all_names = set(_RESOLVER_NAMES)
    assert _NO_YAML_KEY <= all_names
    assert _GENERIC_HELPER_SIGNATURE <= all_names


# ---------------------------------------------------------------------------
# The parity check itself, parametrized over the FULL enumeration.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _RESOLVER_NAMES)
class TestGenericResolverParity:
    def test_yaml_key_is_actually_read(
        self, name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The resolver's return value must CHANGE when its documented yaml
        key(s) are set, relative to an empty/None config. This is the exact
        shape of the #512 bug: a resolver whose yaml key is dead reads the
        same value regardless of what's in athenaeum.yaml.
        """
        if name in _GENERIC_HELPER_SIGNATURE:
            pytest.skip(
                f"{name}: generic parameterized helper, exercised directly "
                "in TestResolveModelDirect below"
            )
        if name in _NO_YAML_KEY:
            pytest.skip(f"{name}: documented to have no yaml key (see module docstring)")

        # Clear every ATHENAEUM_* env var so a stray operator/CI env value
        # cannot mask the yaml-read check.
        for key in list(__import__("os").environ):
            if key.startswith("ATHENAEUM_"):
                monkeypatch.delenv(key, raising=False)

        fn = dict(_ALL_RESOLVERS)[name]
        _env_vars, yaml_keys = _env_and_yaml_literals(fn)
        assert yaml_keys, (
            f"{name}: no yaml key literal discovered in its source (or its "
            "delegated helper). If this resolver genuinely reads no yaml "
            f"key, add it to _NO_YAML_KEY with a one-line justification — "
            "do NOT silently let it pass with nothing checked."
        )

        baseline = _invoke(fn, None, tmp_path)
        candidates = _candidate_nestings(yaml_keys, name)

        changed = False
        errors: list[str] = []
        for candidate_cfg in candidates:
            try:
                result = _invoke(fn, candidate_cfg, tmp_path)
            except Exception as exc:  # noqa: BLE001 — any raise proves the key was read; see comment
                # A resolver that raises on our synthetic sentinel (e.g.
                # resolve_screening validating `action`) is still PROVING it
                # read the key -- just via a loud rejection rather than a
                # changed return value. That counts as "read".
                errors.append(f"{candidate_cfg!r} -> raised {exc!r}")
                changed = True
                break
            if result != baseline:
                changed = True
                break

        assert changed, (
            f"{name}: setting yaml key(s) {yaml_keys!r} (tried "
            f"{len(candidates)} nesting/type combinations) never changed the "
            f"result away from the baseline {baseline!r} — this resolver may "
            "have silently stopped reading its documented yaml key (the "
            f"#512 failure class). Attempts: {errors}"
        )

    def test_env_var_is_actually_read(
        self, name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the resolver references an ATHENAEUM_* env var literal, setting
        it must change the resolver's output (it is documented as
        authoritative over yaml per the module's env > yaml > default
        contract). Resolvers with no env var (by design) are trivially
        skipped -- this is a real, generically-discovered subset, not a
        hand list.
        """
        fn = dict(_ALL_RESOLVERS)[name]

        if name in _GENERIC_HELPER_SIGNATURE:
            pytest.skip(
                f"{name}: generic parameterized helper, exercised directly "
                "in TestResolveModelDirect below"
            )

        env_vars, _yaml_keys = _env_and_yaml_literals(fn)
        if not env_vars:
            pytest.skip(f"{name}: no env var literal discovered (by design)")

        for key in list(__import__("os").environ):
            if key.startswith("ATHENAEUM_"):
                monkeypatch.delenv(key, raising=False)

        baseline = _invoke(fn, None, tmp_path)

        # Env vars are always raw strings (os.environ values), but the knob
        # they feed may be numeric/bool/str-typed underneath, and a value
        # that doesn't parse as that type is (by the module's documented
        # malformed-value policy) WARNed and silently dropped back to the
        # yaml/default -- indistinguishable from "not read" using only one
        # sentinel shape. Try a battery of differently-shaped strings so at
        # least one is well-typed for whatever this knob turns out to be.
        env_sentinel_strings = (
            "__parity_env_sentinel__",
            "123456789",
            "0.987654321",
            "true",
            "false",
        )

        for env_var in env_vars:
            changed = False
            last_result = baseline
            for env_value in env_sentinel_strings:
                monkeypatch.setenv(env_var, env_value)
                try:
                    result = _invoke(fn, None, tmp_path)
                except Exception:  # noqa: BLE001 — any raise proves the env var was read; see comment
                    # A malformed/rejected sentinel causing a validation
                    # error also proves the env var was READ (see rationale
                    # in test_yaml_key_is_actually_read) -- just via a loud
                    # rejection rather than a changed return value.
                    changed = True
                    break
                last_result = result
                if result != baseline:
                    changed = True
                    break
            monkeypatch.delenv(env_var, raising=False)

            assert changed, (
                f"{name}: setting {env_var} to any of "
                f"{env_sentinel_strings!r} never changed the result away "
                f"from baseline {baseline!r} (last tried: {last_result!r}) "
                "— this resolver may have silently stopped reading its "
                "documented env var."
            )


# ---------------------------------------------------------------------------
# resolve_model: exercised directly (generic helper; args are caller-
# supplied, so source-introspection of resolve_model itself can't discover
# a knob/env-var/default — that's the CALLER's business). This is still not
# a per-resolver hand list: it is the ONE parameterized resolver, tested
# with synthetic knob/env names, not real callers' literals.
# ---------------------------------------------------------------------------


class TestResolveModelDirect:
    def test_yaml_knob_is_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_PARITY_PROBE_MODEL", raising=False)
        result = config_mod.resolve_model(
            "parity_probe",
            "ATHENAEUM_PARITY_PROBE_MODEL",
            "code-default",
            {"models": {"parity_probe": "yaml-sentinel"}},
        )
        assert result == "yaml-sentinel"

    def test_env_var_is_read_and_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_PARITY_PROBE_MODEL", "env-sentinel")
        result = config_mod.resolve_model(
            "parity_probe",
            "ATHENAEUM_PARITY_PROBE_MODEL",
            "code-default",
            {"models": {"parity_probe": "yaml-sentinel"}},
        )
        assert result == "env-sentinel"

    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_PARITY_PROBE_MODEL", raising=False)
        result = config_mod.resolve_model(
            "parity_probe", "ATHENAEUM_PARITY_PROBE_MODEL", "code-default", None
        )
        assert result == "code-default"

# SPDX-License-Identifier: Apache-2.0
"""Steered recall under the remote profile (issue athenaeum#2387).

A host with no per-turn injection point (ChatGPT; Tier 3 "pull only" in
``docs/extending/sidecar-adapter-contract.md`` section 3) reads two things and
nothing else: the server ``instructions`` and the tool metadata. This issue
serves a different ``instructions`` text and a different ``recall`` tool
description under ``serve.profile: remote`` — recall-first, and in the first
512 characters OpenAI's Apps SDK guidance says a host actually reads.

One class per acceptance criterion:

- :class:`TestDefaultPathIsByteIdentical` — the criterion that protects every
  existing host. The "before" values are NOT read back off this branch: they
  are the checked-in ``tests/fixtures/pre_2387_served_text.json``, captured
  from a checkout of the base revision that file names.
- :class:`TestRemoteProfileInstructions` — the remote ``instructions`` really
  are what the MCP ``initialize`` result carries, and the 512-character slice
  is taken from THAT served string, never from a copy pasted in here.
- :class:`TestRemoteTextNamesOnlyServedTools` — every tool name the remote
  text mentions is bound to the live remote tool set. Both directions are
  derived from live servers in this process: the tool set absent under the
  profile is ``full - served``, and the authored text's own backticked
  identifiers are extracted from the served bytes.
- :class:`TestNoAutomaticRecallClaim` — a tripwire under the "makes no claim
  of automatic or guaranteed recall" criterion, which is a reviewer's read of
  the text against the design note; the tripwire only catches the phrasings a
  later edit is most likely to reach for.

Regenerating the golden (deliberately, from the BASE revision named in the
fixture — never from this branch): install the package in a clean checkout of
that revision and run

    from pathlib import Path
    import json, tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        knowledge = _build_corpus(root)       # this module's helper, copied in
        print(json.dumps(_capture_served_text(knowledge, root / "cache")))

writing its two keys into the fixture. ``_build_corpus`` is reproduced here
rather than imported so the recipe runs against a revision that does not have
this file.

Unmarked: no network, no model calls, a two-directory temp corpus.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from athenaeum.mcp_server import (
    REMOTE_INSTRUCTIONS_TEMPLATE_NAME,
    REMOTE_RECALL_LEAD_TEMPLATE_NAME,
    remote_server_instructions,
)
from athenaeum.remote_policy import REMOTE_PROFILE, build_remote_policy

_REPO_ROOT = Path(__file__).resolve().parents[1]

#: The "before" values, captured from the base revision. See the module
#: docstring for how to regenerate, and the fixture's own ``_how_to_*`` keys.
_FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "pre_2387_served_text.json"

#: Phrasings that would turn an instruction into a promise. Not an exhaustive
#: content review — that is the reviewer criterion this backs up — but these
#: are the words a well-meaning edit reaches for, and each would be false:
#: nothing recalls on this host unless the model calls the tool.
_GUARANTEE_PHRASINGS = (
    "automatic",
    "automatically",
    "guarantee",
    "guaranteed",
    "every turn",
    "each turn",
    "is injected",
    "will be injected",
    "on your behalf",
)


def _build_corpus(root: Path) -> Path:
    """A one-page knowledge root — the corpus the golden was captured against.

    Deliberately WITHOUT ``wiki/_schema/types.md``, so
    ``declared_entity_classes`` resolves to the empty list and the ``recall``
    description's declared-class string is ``(none yet)`` on both sides of the
    comparison.
    """
    knowledge = root / "knowledge"
    wiki = knowledge / "wiki"
    wiki.mkdir(parents=True)
    (knowledge / "raw").mkdir(parents=True)
    (wiki / "alex.md").write_text(
        "---\nuid: alex\nname: Alex Widget\ntype: person\n---\n\n"
        "Alex Widget works on widget calibration.\n",
        encoding="utf-8",
    )
    return knowledge


def _capture_served_text(knowledge: Path, cache_dir: Path) -> dict[str, str]:
    """The served ``instructions`` and ``recall`` description, off a server.

    The one function both the golden-capture recipe (module docstring) and
    this file's assertions go through, so what is compared is what a client is
    served, read the same way at both ends.
    """
    pytest.importorskip("fastmcp")
    from athenaeum.mcp_server import create_server

    server = create_server(
        raw_root=knowledge / "raw",
        wiki_root=knowledge / "wiki",
        cache_dir=cache_dir,
    )
    return {
        "instructions": server.instructions,
        "recall_description": _tool_descriptions(server)["recall"],
    }


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    return _build_corpus(tmp_path)


@pytest.fixture
def golden() -> dict[str, str]:
    data = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    # Sanity: an empty or truncated fixture would make every equality below
    # pass against nothing.
    assert data["instructions"].strip(), "golden instructions are empty"
    assert data["recall_description"].strip(), "golden recall description is empty"
    assert re.fullmatch(r"[0-9a-f]{40}", data["_base_revision"]), "golden names no revision"
    return data


def _server(corpus_root: Path, **kwargs: object):
    """A server over *corpus_root*, built the way ``athenaeum serve`` builds it."""
    pytest.importorskip("fastmcp")
    from athenaeum.mcp_server import create_server

    return create_server(
        raw_root=corpus_root / "raw",
        wiki_root=corpus_root / "wiki",
        cache_dir=corpus_root.parent / "cache",
        **kwargs,
    )


def _remote_server(corpus_root: Path):
    return _server(corpus_root, remote_policy=build_remote_policy(profile=REMOTE_PROFILE))


def _tools(server) -> list:
    async def _run() -> list:
        return sorted(await server.list_tools(), key=lambda t: t.name)

    return asyncio.run(_run())


def _tool_names(server) -> set[str]:
    return {tool.name for tool in _tools(server)}


def _tool_descriptions(server) -> dict[str, str]:
    return {tool.name: tool.description for tool in _tools(server)}


def _recall_parameters(server) -> dict:
    for tool in _tools(server):
        if tool.name == "recall":
            return tool.parameters or {}
    raise AssertionError("no `recall` tool was served")


def _initialize_instructions(server) -> str | None:
    """The ``instructions`` field of a real MCP ``initialize`` result.

    Goes through FastMCP's own in-memory client and the handshake it performs,
    pinned to the legacy (``initialize``) era so the assertion is against the
    ``InitializeResult`` the acceptance criterion names rather than against
    the modern ``server/discover`` metadata.
    """
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    async def _run() -> str | None:
        async with Client(server, mode="legacy") as client:
            result = client.initialize_result
            assert result is not None, "the legacy handshake returned no InitializeResult"
            return result.instructions

    return asyncio.run(_run())


def _backticked_identifiers(text: str) -> set[str]:
    """The leading identifier of every backticked span in *text*.

    ```recall(type="person")``` yields ``recall``, so a call example binds
    to the same tool name a bare mention does. Both single- and
    double-backtick spans are read, because the two texts this is run over
    follow the conventions of their own neighbourhoods.
    """
    return {m.group(1) for m in re.finditer(r"`{1,2}([A-Za-z_][A-Za-z0-9_]*)", text)}


class TestDefaultPathIsByteIdentical:
    """AC3: without the remote profile, nothing a host reads changed."""

    def test_served_instructions_match_the_pre_change_bytes(
        self, corpus: Path, golden: dict[str, str]
    ) -> None:
        assert _server(corpus).instructions == golden["instructions"]

    def test_served_recall_description_matches_the_pre_change_bytes(
        self, corpus: Path, golden: dict[str, str]
    ) -> None:
        served = _tool_descriptions(_server(corpus))["recall"]
        assert served == golden["recall_description"]

    def test_capture_helper_agrees_with_the_golden_on_both_fields(
        self, corpus: Path, golden: dict[str, str]
    ) -> None:
        """The capture path the golden was produced through, re-run here."""
        captured = _capture_served_text(corpus, corpus.parent / "cache")
        assert captured == {
            "instructions": golden["instructions"],
            "recall_description": golden["recall_description"],
        }

    @pytest.mark.parametrize(
        "policy_kwargs",
        [
            {},
            {"remote_policy": None},
            {"remote_policy": build_remote_policy(excluded_fields=False)},
            {"remote_policy": build_remote_policy(tools=frozenset({"recall", "remember"}))},
        ],
        ids=["omitted", "none", "excluded-fields-off", "tools-allowlist"],
    )
    def test_only_the_profile_switches_the_text(
        self, corpus: Path, golden: dict[str, str], policy_kwargs: dict
    ) -> None:
        """A policy with pins but NO profile is still a default-text server.

        The steering is selected by the profile name, not by "a policy
        exists" — an operator who pins ``serve.excluded_fields`` or
        ``serve.tools`` on their own machine has not asked for remote-host
        steering and must not get it.
        """
        server = _server(corpus, **policy_kwargs)
        assert server.instructions == golden["instructions"]
        assert _tool_descriptions(server)["recall"] == golden["recall_description"]


class TestRemoteProfileInstructions:
    """AC1 and AC2: the packaged text is served, and it steers in the first
    512 characters of what the handshake actually carries."""

    def test_initialize_result_carries_the_remote_instructions(self, corpus: Path) -> None:
        served = _initialize_instructions(_remote_server(corpus))
        assert served == remote_server_instructions()
        # Not vacuous: the default server's handshake carries the other text.
        assert served != _initialize_instructions(_server(corpus))

    def test_first_512_characters_of_the_served_string_steer_to_recall(
        self, corpus: Path
    ) -> None:
        """AC2's slice, taken off the SERVED string.

        The source is the ``initialize`` result, so a text that is correct in
        the packaged file but not actually served fails here.
        """
        served = _initialize_instructions(_remote_server(corpus))
        assert served is not None
        # A slice of a string shorter than the budget would be the whole
        # string, which would make the assertion below say nothing about
        # ORDER — and order is the whole point of the criterion.
        assert len(served) > 512, "the remote text is shorter than the 512-character budget"
        head = served[:512]
        assert re.search(r"call `recall` before answering", head, re.IGNORECASE), head
        # The default text opens on `remember`; this one must not.
        assert "`recall`" in head.split("`remember`")[0]

    def test_remote_recall_description_is_the_lead_plus_the_unchanged_default(
        self, corpus: Path, golden: dict[str, str]
    ) -> None:
        """AC1 for the description half: a prepend, not a rewrite.

        The default description is carried word for word, so the two cannot
        drift apart as the shared half is edited.
        """
        remote = _tool_descriptions(_remote_server(corpus))["recall"]
        assert remote.endswith(golden["recall_description"])
        lead = remote[: len(remote) - len(golden["recall_description"])]
        assert lead.strip(), "the remote description added no lead at all"
        assert re.search(r"call `recall` before answering", lead, re.IGNORECASE), lead

    def test_remote_profile_does_not_change_the_recall_input_schema(self, corpus: Path) -> None:
        """Steering is description-only: the parameter contract is identical,
        argument descriptions included."""
        assert _recall_parameters(_remote_server(corpus)) == _recall_parameters(_server(corpus))

    def test_both_texts_are_packaged_md_files_not_literals(self) -> None:
        """AC1: the text lives under ``src/athenaeum/prompts/``.

        Read through ``importlib.resources``, i.e. the installed package, so
        this passes only if the files really ship — a wheel that dropped them
        fails here rather than at a host's first connection.
        """
        import importlib.resources

        prompts = importlib.resources.files("athenaeum.prompts")
        for name in (REMOTE_INSTRUCTIONS_TEMPLATE_NAME, REMOTE_RECALL_LEAD_TEMPLATE_NAME):
            assert name.endswith(".md")
            assert prompts.joinpath(name).read_text(encoding="utf-8").strip()


class TestPackagedInTheWheel:
    """AC1's packaging half, on the same bar ``tests/test_skill_packaging.py``
    set: a committed file is not a shipped file."""

    def test_pyproject_includes_the_prompts_directory(self) -> None:
        import tomllib

        with (_REPO_ROOT / "pyproject.toml").open("rb") as fh:
            pyproject = tomllib.load(fh)
        include = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["include"]
        assert "src/athenaeum/prompts/**/*.md" in include

    def test_built_wheel_contains_both_steering_texts(self) -> None:
        """Builds a real wheel and looks inside it.

        The declaration above is not the acceptance bar — a build backend that
        stopped honouring it would leave a host connecting to a server whose
        ``instructions`` raise ``FileNotFoundError``, with the config still
        reading correct.
        """
        pytest.importorskip("build", reason="`build` not installed; install the dev extra")
        import subprocess
        import sys
        import tempfile
        import zipfile

        with tempfile.TemporaryDirectory() as outdir:
            # --no-isolation reuses the installed hatchling backend, so this
            # needs no network (same reason tests/test_skill_packaging.py does).
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "build",
                    "--wheel",
                    "--no-isolation",
                    "--outdir",
                    outdir,
                    str(_REPO_ROOT),
                ],
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, (
                f"wheel build failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
            wheels = list(Path(outdir).glob("*.whl"))
            assert len(wheels) == 1, f"expected one wheel, got {wheels}"
            with zipfile.ZipFile(wheels[0]) as zf:
                names = set(zf.namelist())
        for template in (REMOTE_INSTRUCTIONS_TEMPLATE_NAME, REMOTE_RECALL_LEAD_TEMPLATE_NAME):
            packaged = f"athenaeum/prompts/{template}"
            assert packaged in names, (
                f"{packaged} missing from the built wheel. wheel contents (prompts):\n"
                + "\n".join(sorted(n for n in names if "/prompts/" in n))
            )


class TestRemoteTextNamesOnlyServedTools:
    """AC4: every tool name the remote text mentions exists in the remote
    profile's ``tools/list``."""

    def test_no_tool_the_profile_withholds_is_named(self, corpus: Path) -> None:
        """The withheld set is DERIVED — full tool set minus the served one —
        so a tool the profile stops serving tomorrow is covered with no edit
        here."""
        full = _tool_names(_server(corpus))
        served = _tool_names(_remote_server(corpus))
        withheld = full - served
        # Both sanity checks matter: an empty `withheld` would make the loop
        # below vacuous, and an empty text would pass any "not in" assertion.
        assert withheld, "sanity: the remote profile withheld no tool at all"
        remote_text = "\n".join(
            [
                remote_server_instructions(),
                _tool_descriptions(_remote_server(corpus))["recall"],
            ]
        )
        assert remote_text.strip(), "sanity: the remote text is empty"
        for name in sorted(withheld):
            assert name not in remote_text, f"remote text names withheld tool {name}"

    def test_every_tool_name_mentioned_is_served(self, corpus: Path) -> None:
        """Bound in the other direction: each identifier the AUTHORED text
        backticks must be a name on the live remote server.

        Scoped to the authored halves — the remote ``instructions`` and the
        description lead — because the inherited default description also
        backticks backend names (``keyword``, ``fts5``) and a ``uid``, which
        are not tools and never were.
        """
        served = _tool_names(_remote_server(corpus))
        remote_description = _tool_descriptions(_remote_server(corpus))["recall"]
        default_description = _tool_descriptions(_server(corpus))["recall"]
        assert remote_description.endswith(default_description)
        lead = remote_description[: len(remote_description) - len(default_description)]

        mentioned = _backticked_identifiers(remote_server_instructions())
        mentioned |= _backticked_identifiers(lead)
        assert mentioned, "sanity: the authored text backticks no identifier at all"
        unserved = sorted(mentioned - served)
        assert not unserved, f"remote text names tool(s) this profile does not serve: {unserved}"
        # The text is worth steering with: it names several of them.
        assert len(mentioned) >= 3


class TestNoAutomaticRecallClaim:
    """AC5's tripwire: an instruction, never a promise."""

    @pytest.mark.parametrize("phrase", _GUARANTEE_PHRASINGS)
    def test_remote_text_promises_nothing(self, corpus: Path, phrase: str) -> None:
        texts = {
            "instructions": remote_server_instructions(),
            "recall description lead": _tool_descriptions(_remote_server(corpus))["recall"],
        }
        for where, text in texts.items():
            assert phrase not in text.lower(), f"{where} claims {phrase!r}"

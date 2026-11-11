# SPDX-License-Identifier: Apache-2.0
"""Every ``athenaeum`` command shown in an operating doc binds to the real parser.

``docs/reference/cli.md`` is GENERATED from ``athenaeum.cli.build_parser()``
(``scripts/gen_cli_reference.py``, pinned by
``tests/test_generated_docs_parity.py``), so the flag TABLES cannot drift. The
hand-written pages are the gap: a guide shows commands an operator pastes, and
nothing regenerates them. A flag renamed in its ``add_argument(...)`` call
updates the reference page and silently leaves every guide telling the reader
to run something that now exits 2.

This module closes that gap for the pages where a wrong flag is always a bug
-- ``README.md``, ``docs/index.md``, and every page under ``docs/guides/`` and
``docs/modules/``. It extracts each ``athenaeum ...`` invocation out of their
fenced shell and unit-file blocks, resolves the subcommand against the live
argparse tree, and asserts every long option it shows is actually accepted
there. Design notes under ``docs/design/`` are deliberately NOT scanned: a
decision record legitimately discusses a flag that does not exist yet.

The parser tree is read through ``gen_cli_reference``'s own ``_flat_nodes``
walk rather than a second traversal written here, so this check and the
generated reference page can never disagree about what the tree contains.

Three properties the extractor needs, each with a control below:

- **Prose is not a command.** Only fenced blocks are read, and only those
  tagged as a shell or unit-file language, so ``--auth-issuer`` named in a
  sentence is never parsed as an invocation.
- **A wrapped command is one command.** Trailing-backslash continuations are
  joined before parsing, or every flag after the first line is invisible.
- **The binary may not be the first token.** A cron line, a ``systemd``
  ``ExecStart=``, an ``env VAR=x`` prefix and an absolute venv path all put
  real flags behind a prefix. The scan starts at the first token whose
  basename is ``athenaeum``, and short options are never checked, so an
  unrelated line that merely mentions the word contributes nothing.
"""

from __future__ import annotations

import importlib.util
import re
import shlex
from pathlib import Path

import pytest

from athenaeum.cli import build_parser

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS = REPO_ROOT / "docs"

#: The page whose commands this gate exists for. Asserted non-empty on its own
#: so the suite cannot pass vacuously if the guide is renamed or emptied.
REMOTE_GUIDE = DOCS / "guides" / "remote.md"

#: Fenced-block languages whose lines are read as commands. Shell dialects,
#: plus the ``ini`` spelling a systemd unit is fenced with (its ``ExecStart=``
#: carries a real invocation). ``json``/``yaml``/``caddyfile``/``toml`` blocks
#: and untagged blocks (which hold example OUTPUT in this corpus) are skipped.
COMMAND_BLOCK_LANGUAGES = frozenset({"bash", "sh", "shell", "console", "ini", "systemd"})

_FENCE = re.compile(r"^\s*```(.*)$")
_EXEC_START = re.compile(r"^\s*ExecStart(?:Pre|Post)?=")
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
#: A long option as argparse spells it. Short options are deliberately out of
#: scope: ``-la`` on an unrelated line would otherwise read as a flag.
_LONG_OPTION = re.compile(r"^--[a-z0-9][a-z0-9-]*$")
#: Shell noise that can sit between the start of a line and the binary.
_COMMAND_PREFIXES = frozenset({"$", "sudo", "env", "nohup", "exec", "time"})


def _scanned_pages() -> list[Path]:
    return [
        REPO_ROOT / "README.md",
        DOCS / "index.md",
        *sorted((DOCS / "guides").glob("*.md")),
        *sorted((DOCS / "modules").glob("*.md")),
    ]


def _load_gen_cli_reference():
    """Load ``scripts/gen_cli_reference.py`` the way the parity gate does."""
    path = REPO_ROOT / "scripts" / "gen_cli_reference.py"
    spec = importlib.util.spec_from_file_location("gen_cli_reference", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _command_options() -> dict[tuple[str, ...], frozenset[str]]:
    """Map each subcommand path to the option strings it accepts.

    Root options are unioned into every node: ``athenaeum --help`` style
    globals are legal wherever argparse accepts them, and a reader pasting one
    before the subcommand is not making an error this gate should report.
    """
    gen = _load_gen_cli_reference()
    root = build_parser()

    def options(parser: object) -> frozenset[str]:
        return frozenset(
            flag for action in parser._actions for flag in action.option_strings  # type: ignore[attr-defined]
        )

    root_options = options(root)
    nodes = gen._flat_nodes(root, prog="athenaeum")
    assert nodes, "gen_cli_reference._flat_nodes returned no commands; the walk moved"
    table: dict[tuple[str, ...], frozenset[str]] = {(): root_options}
    for node in nodes:
        table[tuple(node.path)] = options(node.parser) | root_options
    assert ("serve",) in table, "expected `athenaeum serve` in the parser tree"
    return table


COMMAND_OPTIONS = _command_options()


def command_lines(text: str) -> list[str]:
    """Every command line inside a fenced command block of *text*.

    Comment-only lines are dropped, a trailing ``# ...`` comment is cut, and
    backslash continuations are joined into one logical line.
    """
    lines: list[str] = []
    in_block = False
    language = ""
    pending = ""
    for raw in text.splitlines():
        fence = _FENCE.match(raw)
        if fence is not None:
            in_block, language = (False, "") if in_block else (True, fence.group(1).strip().lower())
            pending = ""
            continue
        if not in_block or language not in COMMAND_BLOCK_LANGUAGES:
            continue
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = re.split(r"\s+#\s", line)[0].rstrip()
        if pending:
            line = f"{pending} {line}"
        pending = ""
        if line.endswith("\\"):
            pending = line[:-1].strip()
            continue
        lines.append(line)
    return lines


def _is_binary_token(token: str) -> bool:
    return token == "athenaeum" or token.endswith("/athenaeum")


def _invocation(line: str) -> list[str] | None:
    """*line* as an ``athenaeum`` argv, or ``None`` if it is not one.

    A bare ``athenaeum`` token is not enough to call something an invocation:
    the word is also this project's own MCP SERVER NAME, so
    ``claude mcp add --scope user athenaeum -- athenaeum serve --path ...``
    contains it twice and only the second one is the binary. The token must
    therefore be FOLLOWED by something the real parser would accept next --
    a top-level subcommand, or a root-level long option. That is what tells
    the binary apart from a same-named argument, a package name in an install
    line, or a bare ``which athenaeum``.
    """
    line = _EXEC_START.sub("", line)
    try:
        tokens = shlex.split(line)
    except ValueError:
        return None
    subcommands = {path[0] for path in COMMAND_OPTIONS if path}
    root_options = COMMAND_OPTIONS[()]
    for index, token in enumerate(tokens[:-1]):
        if not _is_binary_token(token):
            continue
        following = tokens[index + 1]
        if following in subcommands or following.split("=", 1)[0] in root_options:
            return tokens[index:]
    return None


def unknown_flags(text: str) -> list[str]:
    """Every ``--flag`` *text* shows that its own subcommand does not accept."""
    problems: list[str] = []
    for line in command_lines(text):
        argv = _invocation(line)
        if argv is None:
            continue
        path: tuple[str, ...] = ()
        index = 1
        while index < len(argv) and not argv[index].startswith("-"):
            if (path + (argv[index],)) not in COMMAND_OPTIONS:
                break
            path += (argv[index],)
            index += 1
        accepted = COMMAND_OPTIONS[path]
        command = " ".join(("athenaeum",) + path)
        for token in argv[1:]:
            flag = token.split("=", 1)[0]
            if _LONG_OPTION.match(flag) and flag not in accepted:
                problems.append(f"`{command}` has no {flag} (shown as: {line})")
    return problems


class TestDocumentedCommandsBindToTheParser:
    """The real gate: no operating doc shows a flag the parser rejects."""

    @pytest.mark.parametrize(
        "page", _scanned_pages(), ids=lambda p: str(p.relative_to(REPO_ROOT))
    )
    def test_every_documented_flag_exists(self, page: Path) -> None:
        problems = unknown_flags(page.read_text(encoding="utf-8"))
        assert not problems, (
            f"{page.relative_to(REPO_ROOT)} documents flags the live argparse tree "
            "does not accept; fix the page (or the flag) rather than this test:\n  "
            + "\n  ".join(problems)
        )

    def test_the_remote_guide_contributes_commands(self) -> None:
        """Non-vacuity pin for the remote-mode guide's own acceptance criterion.

        The gate above passes trivially on a page with no commands in it, so
        the one page this check was written for asserts a floor: the guide
        shows real `athenaeum serve` / scheduling invocations, and they are
        the ones being bound.
        """
        text = REMOTE_GUIDE.read_text(encoding="utf-8")
        invocations = [argv for line in command_lines(text) if (argv := _invocation(line))]
        assert len(invocations) >= 5, (
            "docs/guides/remote.md is supposed to show the remote-mode commands, but "
            f"only {len(invocations)} athenaeum invocation(s) were extracted"
        )
        served = [argv for argv in invocations if argv[1:2] == ["serve"]]
        assert served, "the remote-mode guide shows no `athenaeum serve` invocation"


class TestExtractorControls:
    """Positive and negative controls, so a clean tree is not the only evidence.

    Every assertion above would pass identically if the extractor silently
    stopped finding anything. These fixtures prove each of its three documented
    properties independently of the real corpus's current state.
    """

    def test_an_invented_flag_is_caught(self) -> None:
        assert unknown_flags("```bash\nathenaeum serve --not-a-real-flag\n```\n")

    def test_a_flag_on_the_wrong_subcommand_is_caught(self) -> None:
        """`--transport` is real, but only on `serve`."""
        assert unknown_flags("```bash\nathenaeum reindex --transport http\n```\n")

    def test_a_real_command_is_not_caught(self) -> None:
        assert not unknown_flags("```bash\nathenaeum serve --transport http --port 8000\n```\n")

    def test_a_flag_named_in_prose_is_ignored(self) -> None:
        assert not unknown_flags("Pass `--not-a-real-flag` to athenaeum serve to do it.\n")

    def test_a_flag_in_a_non_command_block_is_ignored(self) -> None:
        assert not unknown_flags('```json\n{"cmd": "athenaeum serve --not-a-real-flag"}\n```\n')

    def test_a_wrapped_command_is_joined_before_checking(self) -> None:
        wrapped = "```bash\nathenaeum serve \\\n  --not-a-real-flag\n```\n"
        assert unknown_flags(wrapped), "a continuation line's flags must still be checked"

    def test_a_systemd_exec_start_is_checked(self) -> None:
        unit = "```ini\nExecStart=/opt/athenaeum/venv/bin/athenaeum serve --not-a-real-flag\n```\n"
        assert unknown_flags(unit)

    def test_a_cron_line_is_checked(self) -> None:
        cron = "```bash\n30 3 * * * /opt/athenaeum/venv/bin/athenaeum run --not-a-real-flag\n```\n"
        assert unknown_flags(cron)

    def test_a_trailing_comment_does_not_become_a_flag(self) -> None:
        assert not unknown_flags("```bash\nathenaeum reindex   # --not-a-real-flag\n```\n")

    def test_an_equals_form_flag_is_resolved(self) -> None:
        assert unknown_flags("```bash\nathenaeum serve --not-a-real-flag=1\n```\n")
        assert not unknown_flags("```bash\nathenaeum serve --port=8000\n```\n")

    def test_a_nested_subcommand_resolves_to_its_own_node(self) -> None:
        """`athenaeum storage lint-pii` is two levels deep; `--glob` lives on
        `migrate-pii`, not on `lint-pii`."""
        assert not unknown_flags("```bash\nathenaeum storage lint-pii\n```\n")
        assert unknown_flags("```bash\nathenaeum storage lint-pii --glob 'x*'\n```\n")

    def test_the_server_name_is_not_mistaken_for_the_binary(self) -> None:
        """`athenaeum` is also this project's MCP server name.

        ``README.md``/``install.md`` register it with
        ``claude mcp add ... athenaeum -- athenaeum serve --path ...``. Reading
        the FIRST occurrence as the binary attributes `serve`'s flags to the
        root parser and reports a false positive; this pins the second one.
        """
        line = "claude mcp add --scope user athenaeum -- athenaeum serve --path ~/knowledge"
        assert _invocation(line) == ["athenaeum", "serve", "--path", "~/knowledge"]
        assert not unknown_flags(f"```bash\n{line}\n```\n")

    def test_an_install_line_naming_the_package_is_ignored(self) -> None:
        assert _invocation("uv tool install --python 3.13 athenaeum") is None
        assert _invocation("which athenaeum") is None

    def test_a_non_athenaeum_command_is_ignored(self) -> None:
        assert not unknown_flags("```bash\ncurl -fsS --not-a-real-flag https://example.invalid\n```\n")

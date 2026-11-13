# SPDX-License-Identifier: Apache-2.0
"""A bare ``/`` in an entity name must not mark a page filename-derived (athenaeum#721).

athenaeum#680's ``prune-code-entities`` sweep keyed on the entity name being file-shaped
by *extension OR path separator*. The extension half works; the path-separator
half has no discriminating power in a corpus where slashes are ordinary
punctuation in human and organization names. On the live store it put **140 real
people, companies and concepts on a ``git rm`` kill-list** — 44% of the 315
pages the dry run proposed to delete.

This pins the fix: a slash no longer contributes to the classification at all
(``classify_code_artifact_name`` is extension-only), so every entry in athenaeum#721's
table is retained, while the genuine extension-matched artifacts are still
killed. The AC4 no-extension path case (``src/athenaeum``) is decided
explicitly: retained — see :class:`TestExtensionlessPathDecision`.

The former AC5 class here exercised the one-shot ``prune-code-entities``
sweep (``athenaeum.filename_entity_prune``) directly; that sweep was removed
once the backlog it existed to clear was gone (athenaeum#2466), leaving this
file to pin only the shared ``classify_code_artifact_name``/
``is_code_artifact_name`` predicate the write-side creation gate still uses.
"""

from __future__ import annotations

from athenaeum.tiers import classify_code_artifact_name, is_code_artifact_name

# ---------------------------------------------------------------------------
# AC1 + AC2 — every entry in athenaeum#721's kill-list table is retained
# ---------------------------------------------------------------------------

#: The 140-entry class, taken verbatim from athenaeum#721's table — real people,
#: companies, concepts, packages, skill/label/branch names, and slash-commands
#: that athenaeum#680 wrongly killed solely because the name contains a ``/``. Each MUST
#: be retained (``classify_code_artifact_name`` returns ``None``).
RETAINED_SLASH_NAMES = (
    "Suzie Prince (she/her)",  # a real person — matched on pronouns' slash
    "Alexander Ketelaar / Maykers",  # a person and their company
    "Hans Balmaekers // innov8rs. Balmaekers",  # a person (double slash)
    "Stora Enso / Chalmers University",  # two organizations
    "ITHAKA/JSTOR",  # an organization, no spaces, all-caps
    "Logiticks/Microsoft",  # two organizations, no spaces
    "IxDA/Interaction Design Association",  # an organization
    "IPP Vietnam-Finland / NIRAS",  # a program and partner
    "Innovation Lab (Norway/Dubai)",  # an org, slash inside parens
    "Serbian Government Workshops/Speeches",  # an engagement
    "@tanstack/react-query",  # an npm package — not a file
    "Public Profile / Member Server Terminology Drift",  # a concept page
    "Sync/data-integration lane pre-dispatch checklist",  # a process page
    "dijkstra/arch-review",  # a skill name
    "hestia/needs-human-promotion-review",  # a label name
    "feature/408-gateway-split-write",  # a git branch name
    "feat/daily-briefing-calendar-section",  # a git branch name
    "Kromatic-Innovation/code-workspace-config#333",  # a repo-qualified issue ref
    "/good-morning",  # a slash-command
    "/whats-new page and modal",  # a route
)


class TestBareSlashNoLongerKills:
    def test_every_table_entry_is_retained(self) -> None:
        for name in RETAINED_SLASH_NAMES:
            assert classify_code_artifact_name(name) is None, name
            assert not is_code_artifact_name(name), name

    def test_slash_inside_parentheses_is_never_a_path(self) -> None:
        # AC2 calls this one out specifically.
        assert not is_code_artifact_name("Suzie Prince (she/her)")
        assert not is_code_artifact_name("Innovation Lab (Norway/Dubai)")

    def test_the_narrower_slash_signal_would_have_been_wrong(self) -> None:
        # athenaeum#721 warns: "confirm against the real 140 rather than guess". The
        # suggested narrow signal (slash-separated, no spaces, no @, no capital)
        # would still delete these legitimate skill/label/branch/command names —
        # which is exactly why a slash contributes nothing at all now.
        for name in (
            "dijkstra/arch-review",
            "hestia/needs-human-promotion-review",
            "feature/408-gateway-split-write",
            "feat/daily-briefing-calendar-section",
            "/good-morning",
        ):
            assert not is_code_artifact_name(name), name


# ---------------------------------------------------------------------------
# AC3 — the genuine extension-matched artifacts are STILL killed
# ---------------------------------------------------------------------------


class TestExtensionMatchedStillKilled:
    def test_extension_artifacts_are_killed_with_rule_extension(self) -> None:
        for name in (
            "deploy-guard.sh",
            "wiki_dedupe.py",
            "crawlGovernance.ts",
            "generate-repo-map.sh",
        ):
            assert classify_code_artifact_name(name) == "extension", name
            assert is_code_artifact_name(name), name

    def test_a_full_source_path_with_extension_is_still_killed(self) -> None:
        # A path WITH an extension is matched by its extension, not a path rule.
        assert classify_code_artifact_name("src/athenaeum/librarian.py") == "extension"


# ---------------------------------------------------------------------------
# AC4 — the extension-less path-shaped name: decided explicitly (retained)
# ---------------------------------------------------------------------------


class TestExtensionlessPathDecision:
    def test_extensionless_paths_are_retained(self) -> None:
        # DECISION (athenaeum#721 AC4): a path-shaped name with no extension is RETAINED.
        # No mechanical slash signal separates `src/athenaeum` from the pinned
        # skill/label/branch/command names above, and retention is the safe,
        # reversible direction (deletion is the destructive one). Such a page is
        # still retired the moment it carries a real extension or an operator
        # lists it explicitly.
        for name in ("src/athenaeum", "scripts/oss-export"):
            assert classify_code_artifact_name(name) is None, name
            assert not is_code_artifact_name(name), name


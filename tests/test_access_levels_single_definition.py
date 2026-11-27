"""athenaeum#3159: one definition of the access levels and of the built-in contact fields.

Each derived value is bound to the literal it held before the refactor (copied
here), so the refactor is provably a no-op.
"""

from __future__ import annotations

from importlib import resources

from athenaeum import librarian, models, pii, screening, sensitivity_lint
from athenaeum.models import load_schema_list


def test_access_levels_order() -> None:
    assert models.ACCESS_LEVELS == ("open", "internal", "confidential", "personal")


def test_owner_only_matches_previous_literal() -> None:
    assert models.OWNER_ONLY_ACCESS_LEVELS == frozenset({"internal", "confidential", "personal"})


def test_fallback_access_matches_previous_literal() -> None:
    assert librarian.FALLBACK_ACCESS == ["open", "internal", "confidential", "personal"]
    assert isinstance(librarian.FALLBACK_ACCESS, list)


def test_restricted_levels_match_previous_literal() -> None:
    assert sensitivity_lint._RESTRICTED_ACCESS_LEVELS == frozenset({"confidential", "personal"})


def test_screening_rank_bound_to_constant() -> None:
    # screening keeps a literal (no package import at module scope); bind it here.
    assert screening._ACCESS_RANK == {"open": 0, "internal": 1, "confidential": 2, "personal": 3}
    assert screening._ACCESS_RANK == {lvl: i for i, lvl in enumerate(models.ACCESS_LEVELS)}
    assert list(screening._ACCESS_RANK) == list(models.ACCESS_LEVELS)


def test_shipped_access_levels_scaffold_membership() -> None:
    schema = resources.files("athenaeum") / "schema"
    text = (schema / "access-levels.md").read_text(encoding="utf-8")
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "access-levels.md").write_text(text, encoding="utf-8")
        levels = load_schema_list(Path(d), "access-levels.md")
    assert set(levels) == set(models.ACCESS_LEVELS)
    assert levels == list(models.ACCESS_LEVELS)


def test_contact_fields_match_previous_literals() -> None:
    assert pii.CONTACT_FRONTMATTER_FIELDS == ("emails", "phones")
    assert pii.CONTACT_DATA_FIELDS == ("emails", "phones", "former_emails", "alt_emails")
    assert pii.RECORDABLE_CONTACT_FIELDS == ("emails", "former_emails", "alt_emails", "phones")
    assert pii.CONTACT_IDENTIFIER_FIELDS == ("emails", "former_emails", "alt_emails")


def test_contact_fields_computed_from_one_constant() -> None:
    known = set(pii.BUILTIN_CONTACT_FIELDS)
    for derived in (
        pii.CONTACT_FRONTMATTER_FIELDS,
        pii.CONTACT_DATA_FIELDS,
        pii.RECORDABLE_CONTACT_FIELDS,
        pii.CONTACT_IDENTIFIER_FIELDS,
    ):
        assert set(derived) <= known

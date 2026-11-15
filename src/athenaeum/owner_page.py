# SPDX-License-Identifier: Apache-2.0
"""Create the owner's person page and record the owner in config (athenaeum#2652).

Contract: :func:`set_owner` is the ONE code path behind both
``athenaeum owner set`` and ``athenaeum init --owner-name``. It is idempotent
and non-interactive: it creates the owner's person page (through
:class:`athenaeum.models.WikiEntity`, the same writer model the librarian
uses, uid from :func:`athenaeum.models.generate_uid`), then records
``owner.uid`` and the aliases in ``athenaeum.yaml`` via
:func:`athenaeum.config.write_owner_config`. A second run with the same
arguments writes nothing; a new alias adds only that alias.

Deliberately NOT written: email addresses or phone numbers. Contact data
stays on the excluded surface; this module neither asks for nor stores it.
The page defaults to the ``personal`` access level.

Layering: L3 service. Imports :mod:`athenaeum.config` (L2),
:mod:`athenaeum.models` (L1) and :mod:`athenaeum.atomic_io` (L0).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from athenaeum.atomic_io import atomic_write_text
from athenaeum.config import load_config, resolve_owner, write_owner_config
from athenaeum.models import (
    WikiEntity,
    generate_uid,
    parse_frontmatter,
    render_frontmatter,
)

OWNER_PAGE_ACCESS = "personal"


@dataclass
class OwnerSetResult:
    uid: str
    page_path: Path
    page_created: bool = False
    page_aliases_added: list[str] = field(default_factory=list)
    config_changed: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.page_created or bool(self.page_aliases_added) or self.config_changed


def single_word_alias_warning(alias: str) -> str:
    return (
        f"alias {alias!r} is a single word: the owner name matcher ignores "
        "single-word aliases for display-name matching (it only matches an "
        "explicit handle or process-author field), so it will not bind pages "
        "named by it. Use a full name (two or more words) for name matching."
    )


def _find_page(wiki_root: Path, uid: str) -> Path | None:
    matches = sorted(wiki_root.glob(f"{uid}-*.md"))
    return matches[0] if matches else None


def set_owner(
    knowledge_root: Path, name: str, aliases: list[str] | None = None
) -> OwnerSetResult:
    """Create/refresh the owner page and ``owner:`` config block. Idempotent."""
    name = " ".join(name.split())
    if not name:
        raise ValueError("owner name must not be empty")
    extra: list[str] = []
    for a in aliases or []:
        a = " ".join(a.split())
        if a and a != name and a not in extra:
            extra.append(a)

    wiki_root = Path(knowledge_root) / "wiki"
    wiki_root.mkdir(parents=True, exist_ok=True)

    # ``load_config`` tolerates a malformed ``athenaeum.yaml`` (it falls back to
    # defaults), which here would read as "no owner yet" and mint a fresh uid
    # and person page on every run, orphaning the previous one. Refuse before
    # writing anything instead (Seer review on PR athenaeum#2677).
    config_path = Path(knowledge_root) / "athenaeum.yaml"
    if config_path.exists():
        import yaml

        try:
            yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ValueError(
                f"{config_path} is not valid YAML ({exc.__class__.__name__}); "
                "fix it before running `athenaeum owner set` -- nothing was written"
            ) from exc
    owner = resolve_owner(load_config(Path(knowledge_root)))
    uid = (owner or {}).get("uid") or generate_uid()
    warnings = [single_word_alias_warning(a) for a in [name, *extra] if len(a.split()) < 2]

    page = _find_page(wiki_root, uid)
    result = OwnerSetResult(uid=uid, page_path=page or wiki_root, warnings=warnings)
    if page is None:
        today = date.today().isoformat()
        entity = WikiEntity(
            uid=uid,
            type="person",
            name=name,
            aliases=extra,
            access=OWNER_PAGE_ACCESS,
            created=today,
            updated=today,
            body=f"# {name}\n\nThe owner of this knowledge base.\n",
            source="manual:athenaeum-owner-set",
        )
        page = wiki_root / entity.filename
        atomic_write_text(page, entity.render())
        result.page_path = page
        result.page_created = True
    else:
        result.page_path = page
        meta, body = parse_frontmatter(page.read_text(encoding="utf-8"))
        have = meta.get("aliases")
        have_list = [str(a) for a in have] if isinstance(have, list) else []
        missing = [a for a in extra if a not in have_list]
        if missing:
            meta["aliases"] = have_list + missing
            meta["updated"] = date.today().isoformat()
            atomic_write_text(page, render_frontmatter(meta) + "\n" + body.lstrip("\n"))
            result.page_aliases_added = missing

    result.config_changed = write_owner_config(
        Path(knowledge_root), uid=uid, aliases=[name, *extra]
    )
    return result

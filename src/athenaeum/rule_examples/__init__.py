"""Subpackage holding PACKAGED example shape rules (issue athenaeum#901).

Contract: reference `*.yaml` shape-rule files a human is meant to read,
adapt, and (per `docs/design/shape-rules.md` §5) deliberately graduate out of
`mode: observe`. `athenaeum.rules.load_rules` never reads this subpackage
directly and `athenaeum` never activates a rule from here automatically —
`athenaeum.init.copy_example_rules` (invoked by `athenaeum init
--with-rules`) is the ONLY path these files reach an installed knowledge
root, copying them into `<knowledge_root>/rules/` where `load_rules` does
look. This is the same "packaged example, opt-in copy, never a hardcoded
engine default" shape `athenaeum.templates` already uses for entity-author
scaffolds — see that subpackage's docstring for the precedent.

Factoring rule: only reference `*.yaml` rule files and this docstring
belong here. No Python logic. Sits below L1 (no imports of its own).
"""

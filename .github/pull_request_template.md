## Summary

<!-- 1-3 bullets describing what and why. -->

## Changes

<!-- List of substantive changes. -->

## Test plan

- [ ] `pytest tests/ -v`
- [ ] `ruff check src/ tests/`
- [ ] Manually exercised any behaviour this changes

## Hook changes only

<!-- Delete this section if the PR doesn't touch scripts/hooks/ or examples/claude-code/. -->

- [ ] Ran the hook against a real UserPromptSubmit payload in a fresh shell
- [ ] Confirmed injected `additionalContext` appears in the model reply
- [ ] Verified both `SEARCH_BACKEND=fts5` and `SEARCH_BACKEND=vector` (if applicable)

## Related issues

<!-- Closes #123, refs #456 -->

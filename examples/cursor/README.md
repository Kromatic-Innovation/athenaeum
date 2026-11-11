# Cursor instructed-recall rule (example)

`athenaeum.mdc` is an example Cursor project rule that tells the agent when
to call athenaeum's `recall` and `remember` MCP tools. It is a template: the
repo installs nothing, and loading it into a Cursor project or user rules
directory is an operator step.

1. Copy the file into `.cursor/rules/` inside a project (a project rule), or
   into Cursor's user rules location (a user rule applied across projects).
2. Restart Cursor, or start a new chat, so the rule is picked up.

This is instructed recall, not automatic per-turn recall: Cursor applies the
rule to every chat session, but whether the agent calls `recall` or
`remember` still depends on the model following the instruction. Full
context and limitations are in
[docs/guides/cursor.md](../../docs/guides/cursor.md).

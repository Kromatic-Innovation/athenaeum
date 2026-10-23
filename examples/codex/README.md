# Codex Tier-2 context-file adapter (example)

`com.athenaeum.codex-context.plist` is an example launchd LaunchAgent that
re-runs `athenaeum codex-context` every 900 seconds. It is a template: the
repo installs nothing, and loading it is an operator step.

1. Replace every `REPLACE_ME` (venv path, seed topics, home directory).
2. Keep `StartInterval` equal to `--interval-seconds`.
3. Copy it to `~/Library/LaunchAgents/` and load it with `launchctl`.
4. Add the pointer line to `~/.codex/AGENTS.md` yourself.

The unit must use the venv's absolute `bin/athenaeum` and pass `--cache-dir`
(launchd has no login-shell environment). Full semantics, staleness behaviour
and the egress rule are in [docs/guides/codex.md](../../docs/guides/codex.md).

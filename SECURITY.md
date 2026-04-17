# Security Policy

## Supported versions

Athenaeum is pre-1.0. Only the latest release on the `develop` branch is supported with security patches.

## Reporting a vulnerability

Please report security vulnerabilities privately via email to **open-source@kromatic.com** with the subject prefix `[athenaeum security]`. Do **not** open a public GitHub issue for security problems.

Include:

- A description of the vulnerability and its impact
- Steps to reproduce (ideally a minimal failing test or PoC)
- Affected version or commit SHA

We aim to:

- Acknowledge your report within 3 business days
- Confirm the vulnerability (or explain why it isn't one) within 10 business days
- Ship a fix and public advisory within 30 business days of confirmation

## Scope

Athenaeum writes files to a user-controlled knowledge directory and makes authenticated calls to the Anthropic API. Security-relevant areas include:

- **Prompt injection** against the tiered LLM pipeline (Tier 2/3) and the `query-topics` preprocessor
- **Path traversal** via entity filenames or `source` parameters
- **API key handling** — we expect callers to source keys from their own secret stores; don't embed keys in configuration or logs
- **MCP server input handling** — the `remember` / `recall` tools accept arbitrary text from AI agents

The raw intake is intentionally append-only. Attacks that rely on overwriting trusted entities are out of scope unless they bypass the append-only invariant.

## Out of scope

- Issues in `[vector]` dependencies (report upstream to chromadb)
- Issues in the Anthropic SDK (report upstream to anthropic-sdk-python)
- Social-engineering attacks against maintainers or downstream adopters

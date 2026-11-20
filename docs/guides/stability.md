**Reference:** [mcp](../modules/mcp.md) · [librarian](../modules/librarian.md)

# What counts as stable

Athenaeum is pre-1.0 and follows semantic versioning loosely: a minor release
(0.x) may make breaking changes, and breaking changes ship without a deprecation
period. This page says which surfaces you can build on and which you cannot. For
the release cadence and what "supported" means, see the
[support policy](install.md#support-policy).

## The compatibility promise

Only the names in `athenaeum.__all__` carry a compatibility promise. A
breaking change to one of them is a deliberate, CHANGELOG-announced event.

## Documented surfaces

These surfaces are documented in the reference pages. They are the ones to
build on, but they are not covered by the `__all__` promise: a release can change
them, and the [CHANGELOG](../../CHANGELOG.md) is where the change is announced
(under `### Removed` or `### Changed`). Read it before upgrading across a minor.

| Surface | Where it is documented | What to expect |
|---|---|---|
| CLI commands and flags | [CLI reference](../reference/cli.md) | Generated from the real argparse tree, so the page is always current. A command can be removed or renamed in a minor release; see [Upgrading](upgrading.md) for the mapping. |
| Config keys | [Configuration reference](../reference/configuration.md) | Generated from the `resolve_*` functions. Keys that have no effect are removed rather than deprecated. |
| MCP tool names and schemas | [The MCP surface](../modules/mcp.md) | The tool list and argument shapes are documented there. A tool or argument can change in a minor release, so pin a version if a client depends on a schema. |
| On-disk file formats (raw intake, wiki frontmatter, run artifacts) | [Data formats](../reference/data-formats.md) | Existing knowledge directories are read by later releases; what a release does to an existing wiki is described in [Upgrading](upgrading.md). |
| Exit codes and environment variables | [Exit codes](../reference/exit-codes.md), [Environment variables](../reference/environment.md) | Documented; changes are announced in the CHANGELOG. |

## Experimental surfaces

These are marked experimental where they are documented. They sit outside the
`__all__` promise and may change or be removed in any minor release without a
deprecation cycle.

- Remote serving (`athenaeum serve --transport http`) and the `serve.auth.*`
  keys, see [Remote mode](remote.md).
- The bundled `promptwitness` package.
- The source trust graph and the commands and keys that configure it.

## Internal

Everything else is internal and may change at any time without notice:

- Every importable module and name that is not in `athenaeum.__all__`.
- CLI flags, config keys, MCP tool fields and file contents that no reference
  page documents.
- Generated state under a knowledge directory (indexes, caches, sidecar files,
  lock files). Read them through the CLI or MCP tools, not directly.
- Log and diagnostic message wording.

If you need a firmer promise for one of the documented surfaces, open an issue
asking for it.

**Reference:** [librarian](../modules/librarian.md)

# Nightly cost and page recompile

The nightly librarian run spends model calls. The largest optional piece is
the page-recompile pass, which restructures oversized pages automatically and
is **on by default**. This page states what it costs and how to bound or turn
it off. For the full operating procedure see
[Daily operation](daily-operation.md).

## Page recompile

A page that
keeps growing becomes a page nobody can read and the compiler cannot merge into cleanly.
Every night the
librarian looks for pages over `librarian.page_size_threshold_chars` (the one setting that
decides which pages are picked up) and cuts the biggest few into a parent page plus linked
sub-pages. **This costs model calls every night**: up to
`librarian.page_recompile.max_pages_per_run` pages a run (3, or `opening_max_pages_per_run` —
10 — while any page is still over the hard ceiling), bounded further by
`max_input_chars_per_run` and `max_api_calls_per_run`. A plan that only restructures the page
itself — nothing moved to another page, nothing attached, no alias moved — is applied right
then, with no question asked; a plan that touches another page, or names a page `type:` you
listed in `never_auto_types`, is still raised in `athenaeum decisions` as a `page-recompile`
item for you to approve or reject, and stops raising new ones once `max_open_items` (5) are
unanswered. **See what the pass has done** with `athenaeum recompile list --applied` or
`athenaeum status` (which fills "applied in the last 7 days"); **undo one** with
`athenaeum recompile undo <plan-id>` — reversible either way, auto-applied or
operator-approved. **To ask before every write, or turn it off entirely, set
`librarian.page_recompile.mode`:**

```yaml
librarian:
  page_recompile:
    mode: queue   # off | shadow | queue | auto (default) -- queue asks before every write
```

`shadow` keeps drafting plans and recording them without asking or writing, which is the
cheapest way to see what the pass would do.

See [Daily operation](daily-operation.md) for the knobs, modes and undo
procedure in full.

# Observation Filter

Meta-memory: guides what the system notices and saves. This file is both
configuration and a living document — the librarian updates it during
consolidation when patterns emerge in incoming data, and Claude updates it
directly when the user gives feedback about what should or shouldn't be
captured (see **Tuning** below).

## Always Capture

These categories are always worth saving when encountered:

- **People**: names, roles, relationships, contact details, reliability notes
- **Decisions**: architectural choices, tool selections, process changes, and their rationale
- **Corrections**: when the user corrects the agent's understanding — the correction AND the wrong assumption
- **Preferences**: communication style, workflow preferences, scheduling patterns, tool opinions
- **Principles**: stated values, guiding rules, axioms the user operates by

## Capture When Reinforced

These categories are saved when the user signals they matter (by explicitly
asking to remember, or when three or more related observations accumulate):

- **Food & dining**: restaurant preferences, dietary constraints
- **Travel**: frequent destinations, airline/hotel preferences
- **Health**: relevant context for scheduling or energy management
- **Hobbies & interests**: non-work topics the user engages with

## Never Capture

Exclusions added here take precedence over the sections above. Populate with
user feedback like "stop saving X" or "you're being too noisy about Y".

<!-- Example:
- **Error message snippets**: dumps of stack traces and command output — these
  are ephemeral and belong in retros, not the wiki (added 2026-04-17 per user).
-->

## Tuning

This filter is user-tunable. When the user gives feedback about what should
or shouldn't be captured, update this file directly (Claude's Edit tool is
fine; `athenaeum run` will pick up the change on the next consolidation pass).
Log the change with a date and a one-line rationale so future readers can
trace why an entry exists.

Signals to watch for and how to respond:

| User signal                        | Where to put it            |
| ---------------------------------- | -------------------------- |
| "stop saving X" / "too noisy on Y" | **Never Capture**          |
| "you should have remembered Z"     | **Always Capture**         |
| "remember more about <topic>"      | **Capture When Reinforced**|
| "Z matters a lot" (after reinforce)| promote to **Always Capture** |

After editing this file, save a brief meta-observation via the `remember` tool
(e.g., "Updated observation-filter: added 'Never Capture: stack traces' per
user feedback") so the tuning is audit-trailed in raw/.

## Decay Rules

- Short-term filter items (added by the librarian from pattern detection)
  decay after 30 days unless reinforced by new observations.
- Items in "Always Capture" do not decay.
- Items promoted from "Capture When Reinforced" to "Always Capture" require
  human approval.

## Pattern Detection

During consolidation, the librarian analyzes the last N raw intake files
and proposes filter additions when:

- 3+ observations in the same category arrive within 7 days
- A user explicitly says "remember X" about a topic not yet in the filter
- A source entity accumulates 5+ citations (signals the source is important)

Proposed additions are appended to `_pending_questions.md` for human review.

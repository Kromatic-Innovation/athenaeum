# Observation Filter

Meta-memory: guides what the system notices and saves. This file is both
configuration and a living document — the librarian updates it during
consolidation when patterns emerge in incoming data.

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

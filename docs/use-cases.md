<!-- SPDX-License-Identifier: Apache-2.0 -->

# Use cases — what "the right information" means concretely

**Status:** PURPOSE DOCUMENT, companion to [`north-star.md`](north-star.md).
Ratified by the operator on 2026-09-16. The north star is a quality bar; this
page decomposes it into the questions a memory system is actually asked, so
that each one can be represented by an eval and the bar becomes falsifiable.

Two tiers. **Current** use cases are backed by evals now, or are the direct
next eval to build. **Aspirational** use cases are not implemented and have no
evals; they exist so the architecture does not paint itself into a corner
that makes them impossible later.

---

## 1. The kill criterion

Athenaeum is justified only if it answers the current use cases **better than
the host agent's own native memory** at acceptable cost. If it does not, the
project should be shelved rather than extended. This is the single most
important eval in the repo and it is specified in
[`design/native-memory-baseline.md`](design/native-memory-baseline.md).

"Better" is defined there, not here, but in one line: win the relationship
use case (§2.1), do not lose the other three, and hold at the corpus scale a
real deployment reaches.

## 2. Current use cases

These are the four questions from [`why-athenaeum.md`](why-athenaeum.md),
made concrete. Each names the probe classes in the synthetic corpus
(`tests/evals/data/corpus/`, taxonomy in `tests/evals/corpus.py`) that grade
it. Every example below is invented.

### 2.1 What is our relationship with this person or company?

The first-class use case: the consumer is a relationship-management workflow
that needs to know who someone is, how they are connected, what has happened
with them, and what is current, before deciding whether and how to reach out.

| The agent asks | Memory must | Probe class |
|---|---|---|
| "Who is Priya?" (first name only) | Resolve an alias to one entity, not create a duplicate | `single_hop`, `disambiguation` |
| "What is our history with Thornhollow Advisory?" | Assemble engagements, contacts, and decisions linked to one company | `multi_hop` |
| "Is this address still current?" | Return the value, its provenance, its usage classification, and validity dates, as facts, not a verdict | `temporal` |
| "Which of these two conflicting titles is right?" | Show the dispute marker and the precedence, never silently pick | `disambiguation` |
| "What do we know about a company we have never met?" | Say nothing, confidently | `abstention` |

Sensitivity is part of this use case, not a separate one: contact data is
routinely excluded from the corpus and reachable only through the one
read path with the excluded-fields knob (north star §2.6, §2.10).

### 2.2 Why are we doing this?

Strategy and goals, so an autonomous flow or a returning human can link an
action to the objective it serves.

| The agent asks | Memory must | Probe class |
|---|---|---|
| "What is the goal this project serves?" | Return the current goal page, not a superseded one | `single_hop`, `temporal` |
| "Does this action still advance the goal?" | Surface the goal and the constraints that bound it together | `multi_hop` |

### 2.3 Why did we decide this?

Decisions and the evidence behind them, so agents and humans stop
re-deciding and contradicting each other.

| The agent asks | Memory must | Probe class |
|---|---|---|
| "Why did we choose vendor X?" | Return the decision with its sources and the alternative it rejected | `single_hop`, `multi_hop` |
| "Has this decision been reversed?" | Rank the newer decision above the superseded one; mark the supersession | `temporal` |
| "Who made this call?" | Attribute the decision to its asserter and date, not to the agent that compiled it | `single_hop` |

### 2.4 What did we learn last time we tried this?

Retrospectives and recurring mistakes, so the same one is not repeated.

| The agent asks | Memory must | Probe class |
|---|---|---|
| "We are about to do X again. What went wrong before?" | Surface the retro without being asked by name | `multi_hop`, `distractor_robustness` |
| "Is this the same failure as last month's?" | Match on the pattern, not the wording | `distractor_robustness` |

### 2.5 Cross-cutting requirements

These apply to every use case above and map to the three north-star failure
directions.

- **Passive delivery.** The right page reaches the agent without the agent
  deciding to look (why-athenaeum §3). Measured as the no-call rate in the
  PULL arm of the north-star comparison (`tests/evals/rollout.py`).
- **Follow-through.** A breadcrumb is only useful if the agent opens the page
  behind it and follows its links. The realistic question is one whose full
  answer needs a technical detail two pages away from what was surfaced, so
  the comparison must include multi-turn probes of that shape
  (`design/native-memory-baseline.md` §5). No such probe class exists yet.
- **Currency.** Something reported today is recallable today, not after the
  next nightly run (north star §1, "too late"). No eval covers this yet; it is
  named here so its absence is visible.
- **Not drowned.** Telemetry and short-term state never outrank durable
  knowledge (north star §1, "drowned"; §2.3; §2.5). Graded by the
  `distractor_robustness` class and the `abstention` class.

## 3. Aspirational use cases

Not implemented. No evals. These exist to constrain the architecture so the
single-operator deployment of today does not become the ceiling. They
describe an organisation, not a person.

### 3.1 Roll-up: a division librarian compiles team wikis

Several teams each run their own librarian and wiki. A division-level
librarian reads those team wikis as sources and compiles a higher-level wiki:
facts that hold across teams, contradictions between teams, decisions that
affect more than one. Provenance chains through every level, so a claim on
the division page cites the team page, which cites the raw observation.

### 3.2 Push-down: a leadership decision propagates to team wikis

A decision made at the top enters the one way in as a high-authority claim
and becomes visible in every team wiki it applies to. A team cannot silently
override it (source precedence, north star §2.6, conflicts module), but a
team's contradicting observation surfaces in the decision queue of whoever
owns the decision, not in the team's own queue.

### 3.3 Private to shared: a member's memory feeds a team wiki

An individual's memory is theirs. A subset of it, under rules they control,
contributes to the team wiki. Audience scoping fails closed at every level:
an untagged fact stays private.

### 3.4 Cross-team contradiction surfaces to the right human

Two teams hold conflicting facts about the same real-world subject. The
contradiction is detected once, surfaces in one queue, and is routed to a
human with authority over both, rather than to both teams separately or to
neither.

### 3.5 What these impose on the architecture now

- **Scope is a typed coordinate, never a filesystem path.** Origin scope is
  provenance; claimed scope is an asserted coordinate. This is the reshaped
  form of the retired path-derived scope model and is carried by the
  dimensional memory model (athenaeum#709).
- **Provenance must survive compilation.** A compiled page is itself a
  source for the level above, so per-claim sources cannot be flattened into a
  page-level footnote.
- **Audience scoping is per level, and fail-closed at each.** The current
  single-operator read filter is the degenerate one-level case of this.
- **Identity is not a singleton.** Nothing new should assume one owner uid.

## 4. What is not a use case

- A single-user chatbot that needs only conversational continuity.
- Telemetry, run logs, or short-lived state. These are dispositioned, not
  remembered (north star §2.3).
- Deciding what a caller may do with a fact. Consumers own their policies
  (north star §2.6).

## 5. Using this document

A proposed eval that grades none of the questions in §2 is measuring
something the project does not exist for; either name the use case it serves
or file it as operational measurement. A proposed design that would make a
§3 use case impossible needs an explicit decision to drop that use case, not
a silent one.

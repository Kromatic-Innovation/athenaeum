# Synthetic knowledge corpus

A knowledge tree with known ground truth, at controllable scale, carrying no
content from any live knowledge tree.

## Content policy (a review blocker, not a guideline)

Every entity here is **invented**. Nothing is copied, paraphrased, sampled, or
LLM-rewritten from a live knowledge tree. Only **distribution parameters** are
taken from one -- page-length histogram, links per page, `claim_kind` mix,
entity-type frequencies, contradiction rate, supersession patterns -- via
`scripts/measure_corpus_shape.py`, whose output is **gitignored and never
committed**. A frequency table over a personal corpus is not reliably
content-free, and committing it would place the derived artifact in the very
directory the PII lint scans.

`tests/test_eval_corpus_leakage.py` enforces this on every PR. (Not
`tests/test_corpus_pii_lint.py` -- that one gates the *live* knowledge corpus
for inline contact data, athenaeum#495. Same word, two corpora.) It runs offline and
checks generated and hand-authored pages alike against a denylist **read from
the local knowledge tree at lint time and never written to disk** -- a
committed denylist of real proper nouns would itself be the leak it exists to
prevent. With no local tree present the check skips loudly rather than
passing silently, so an outside contributor's suite still runs.

### What the guards catch, and what they cannot

Five checks run offline on every PR (`tests/test_eval_corpus_leakage.py`), and
each has a negative control proving it fails on the leak it claims to catch:

| check | catches |
|---|---|
| multi-word name denylist | a real person or org name, read from the whole tree |
| structural-name check | any token the corpus *leans on* (>=3 pages) matching a real entity name |
| exhaustive name-space check | every one of the ~13,500 composable generated names |
| brand list | well-known real commercial products |
| path + contact scans | local paths, emails, phone-shaped tokens |

**Known limitation, stated rather than papered over:** a single-use,
single-token real brand *not on the curated list* cannot be caught
mechanically. The structural check needs three uses, and matching every real
company name is unusable -- the tree holds 2,666 company-name tokens including
`about`, `access`, `data` and `first`, which produced 170 hits that were almost
entirely ordinary English. That residue is a review responsibility, not an
automated guarantee.

Two historical failures are worth knowing, because both passed every check
green at the time:

1. The denylist scanned `sorted(...)[:4000]` of 25,487 files. Pages are named
   by hex uid, so that was not a sample but "uids beginning 0, 1 or 2" -- the
   same 16% every run. Removing the limit cost 0.9s.
2. Matching was whole-phrase, so a real person's full name could never collide
   with a fixture character sharing only their surname. The corpus had built
   its entire cast on a real person's surname without any check noticing.

### The identity-collision cast

`core/04-identity-collision.yaml` reproduces the *structure* of a real
observed retrieval failure -- a person, a code repository named after that
person, and same-surname relatives competing for one name query -- using
entirely invented entities. **Describe that failure structurally; never name
the real entities**, in fixtures, issues, commit messages, or code comments.

## Three tiers

| tier | source | purpose |
|---|---|---|
| `core` | hand-authored `core/*.yaml`, committed | carries every ground-truth assertion |
| `distractor` | generated from each probe's `distractor_terms` | creates retrieval *pressure* |
| `ballast` | generated, topic-diverse | gives the index realistic *size* |

Authored as a handful of world files rather than a hundred loose markdown
pages so the whole corpus can be audited by reading top to bottom.

## Relatedness and redundancy (issue athenaeum#1570)

Two clusters live in `core/08-relatedness.yaml`, and they want **opposite
verdicts**. That is the point: a fixture conflating them would license a change
that papers over one while appearing to fix the other.

| cluster | shape | ground truth |
|---|---|---|
| **A — related, unlinked** | a concept, a model *of* it, a process *embodying* it, with zero edges between them | the three edges that **should** exist |
| **B — redundant** | a bare name, the same name with a parenthetical qualifier, and an associated team page | the first two **merge**; the team page **does not** |

Cluster B's team page is the **negative control**. Without it, "merge
everything sharing this name" scores perfectly — the redundancy analogue of
linking everything. The merge *verdict* is graded in athenaeum#1577; what is
graded here is the **retrieval cost** of the split, via the `redundancy` probe
`keelbridge_programme_scope`: a complete answer needs both halves and retrieval
delivers one.

**Not a fourth tier, and the edges are not in the pages.** `core` is defined as
the tier carrying every ground-truth assertion, and these clusters are ground
truth; `distractor` and `ballast` are held apart because each is a *generated*
axis a regression can be attributed to, and relatedness generates nothing. The
edges themselves live in `ground_truth/relatedness.yaml` rather than on the
pages, because the fixture's entire value is that they are **absent** — a
corpus that already carries them cannot tell a librarian that writes edges from
one that does not.

The measure (`tests/evals/relatedness.py`) counts spurious edges against the
**whole corpus**, not just the other members of the cluster. A measure that
only rewards more edges is maximised by linking every page to every other page,
which is the failure the viewer already documents at `_cmd_viewer.py:577-580`:
breadcrumb colour stops carrying information once everything is a breadcrumb.
`tests/test_eval_corpus_relatedness.py` pins both directions — link-nothing
fails on recall, link-everything fails on precision — plus a positive control,
and asserts their *ordering* rather than a tuned constant.

## Two axes, deliberately independent

Size and confusability are separate knobs (`tests/evals/corpus.py`, `SCALES`).
Scaling them together is the intuitive move and it destroys the result.

Ballast alone does not compete for rank -- BM25 will not surface a page that
shares no terms with the query -- so a corpus scaled only with ballast can
reach 10k pages with recall@k untouched, licensing the wrong conclusion that
scale is harmless. Near-miss pages are what actually crowd out a correct
answer. Holding the axes apart is what lets a result distinguish *"the corpus
is too big"* from *"the corpus is too confusable"* -- different problems, with
different fixes (a better index vs better disambiguation).

`medium`, `medium_dense` and `medium_verydense` all hold 1,000 pages and vary
only near-miss density; `small`/`medium`/`large` hold density and vary size.

## Reproducibility

Generation is deterministic and LLM-free: a seeded PRNG slot-fills committed
templates. `(GENERATOR_VERSION, seed, scale)` reproduces a byte-identical
tree, so large corpora are generated on demand and **never enter git** -- which
is also why this corpus does not need to be its own repository.

An LLM generator was rejected deliberately: it would cost per page at 10k
scale, make runs unreproducible, and -- the real reason -- an LLM writing
"realistic" pages is precisely the paraphrase-leakage path the content policy
forbids.

Record `Corpus.fingerprint()` with every measurement. A seed alone stops
identifying a corpus the moment the generator or the hand-authored core
changes.

## Usage

```python
from tests.evals.corpus import build_corpus

corpus = build_corpus(scale="core")          # offline default, no generation
wiki_root = corpus.materialize(tmp_path)     # a real wiki tree
```

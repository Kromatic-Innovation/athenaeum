# Write-knob tier comparison — STUB DATA (athenaeum#1139)

**This table was generated against a FAKE/canned provider (`tests.conftest.FakeLLMClient`) — no live Anthropic call was made. It exists only to prove the runner/scorer/table generator work end-to-end. It carries no information about real model quality, cost, or latency and MUST NOT be used for a tier downgrade decision.** The real table needs `ANTHROPIC_API_KEY` and is produced by `pytest tests/evals/test_write_tier_compare.py -m eval`.

_Generated 2026-09-01T06:10:37.076248+00:00_

| model | case | kind | entity | pass | input_tok | output_tok | cost_usd | wall_clock_s | detail |
|---|---|---|---|---|---|---|---|---|---|
| claude-sonnet-5 | simple_create_single_person | simple_create | Dana Whitfield | PASS | 350 | 90 | 0.0024 | 0.000 | ok |
| claude-sonnet-5 | multi_entity_create_person_and_project | multi_entity_create | Marcus Oyelaran | PASS | 350 | 90 | 0.0024 | 0.000 | ok |
| claude-sonnet-5 | multi_entity_create_person_and_project | multi_entity_create | Beacon Compliance Review | PASS | 350 | 90 | 0.0024 | 0.000 | ok |
| claude-sonnet-5 | merge_small_page_new_fact | merge_small | merge_small_page_new_fact | PASS | 700 | 180 | 0.0048 | 0.000 | ok |
| claude-sonnet-5 | merge_large_page_preserve_content | merge_large | merge_large_page_preserve_content | PASS | 350 | 90 | 0.0024 | 0.000 | ok |
| claude-haiku-4-5 | simple_create_single_person | simple_create | Dana Whitfield | PASS | 350 | 90 | 0.0008 | 0.000 | ok |
| claude-haiku-4-5 | multi_entity_create_person_and_project | multi_entity_create | Marcus Oyelaran | PASS | 350 | 90 | 0.0008 | 0.000 | ok |
| claude-haiku-4-5 | multi_entity_create_person_and_project | multi_entity_create | Beacon Compliance Review | PASS | 350 | 90 | 0.0008 | 0.000 | ok |
| claude-haiku-4-5 | merge_small_page_new_fact | merge_small | merge_small_page_new_fact | PASS | 700 | 180 | 0.0016 | 0.000 | ok |
| claude-haiku-4-5 | merge_large_page_preserve_content | merge_large | merge_large_page_preserve_content | PASS | 350 | 90 | 0.0008 | 0.000 | ok |

## Per-model summary

| model | cases | passed | total_input_tok | total_output_tok | total_cost_usd | avg_wall_clock_s |
|---|---|---|---|---|---|---|
| claude-sonnet-5 | 5 | 5/5 | 2100 | 540 | 0.0144 | 0.000 |
| claude-haiku-4-5 | 5 | 5/5 | 2100 | 540 | 0.0048 | 0.000 |

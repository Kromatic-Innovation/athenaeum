# Write-knob tier comparison (athenaeum#1139)

_Generated 2026-09-03T09:57:44.560829+00:00_

| model | case | kind | entity | pass | input_tok | output_tok | cost_usd | wall_clock_s | detail |
|---|---|---|---|---|---|---|---|---|---|
| claude-sonnet-5 | simple_create_single_person | simple_create | Dana Whitfield | PASS | 792 | 286 | 0.0067 | 4.726 | ok |
| claude-sonnet-5 | multi_entity_create_person_and_project | multi_entity_create | Marcus Oyelaran | PASS | 789 | 352 | 0.0076 | 6.017 | ok |
| claude-sonnet-5 | multi_entity_create_person_and_project | multi_entity_create | Beacon Compliance Review | PASS | 793 | 329 | 0.0073 | 6.020 | ok |
| claude-sonnet-5 | merge_small_page_new_fact | merge_small | merge_small_page_new_fact | PASS | 1641 | 234 | 0.0084 | 3.000 | ok |
| claude-sonnet-5 | merge_large_page_preserve_content | merge_large | merge_large_page_preserve_content | PASS | 7697 | 186 | 0.0259 | 4.925 | ok |
| claude-haiku-4-5 | simple_create_single_person | simple_create | Dana Whitfield | PASS | 565 | 189 | 0.0015 | 2.337 | ok |
| claude-haiku-4-5 | multi_entity_create_person_and_project | multi_entity_create | Marcus Oyelaran | PASS | 563 | 241 | 0.0018 | 2.859 | ok |
| claude-haiku-4-5 | multi_entity_create_person_and_project | multi_entity_create | Beacon Compliance Review | PASS | 568 | 156 | 0.0013 | 2.311 | ok |
| claude-haiku-4-5 | merge_small_page_new_fact | merge_small | merge_small_page_new_fact | PASS | 1161 | 180 | 0.0021 | 2.145 | ok |
| claude-haiku-4-5 | merge_large_page_preserve_content | merge_large | merge_large_page_preserve_content | PASS | 4999 | 206 | 0.0060 | 2.752 | ok |

## Per-model summary

| model | cases | passed | total_input_tok | total_output_tok | total_cost_usd | avg_wall_clock_s |
|---|---|---|---|---|---|---|
| claude-sonnet-5 | 5 | 5/5 | 11712 | 1387 | 0.0559 | 4.937 |
| claude-haiku-4-5 | 5 | 5/5 | 7856 | 972 | 0.0127 | 2.481 |

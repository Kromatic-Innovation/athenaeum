## Cost per correct answer (athenaeum#1734)

`cost_per_correct = (read_input_tokens + read_output_tokens + write_tokens_amortized) / correct_n` -- read cost only in Phase 1. Phase 2 adds write cost amortised over the full probe set at that scale (design doc §6); for a non-native arm `write_tokens_amortized` is the native writer's own amortised share PLUS the librarian's filing share (athenaeum#1870, ruling athenaeum#1830), never the filing share alone -- `write_native_share_amortized` prints that native portion separately so neither component is hidden, and the raw (unamortised) sum is printed alongside. `n/a` in the native-share columns means either no write data at all, or (for a native arm) that the whole combined figure already is native, so there is no separate share to print; `not-captured` means an Athenaeum arm's native share specifically was never captured, so the figure shown is filing-only -- see the design doc. `undefined` (never `inf`/`0`) means zero correct answers in the cell -- there is no denominator.

| probe_class | corpus_scale | arm | n | correct_n | read_tokens | cost_per_correct |
| --- | --- | --- | --- | --- | --- | --- |
| single_hop | core | oracle | 1 | 1 | 100 | 100.0 |
| single_hop | core | pull | 1 | 0 | 60 | undefined |

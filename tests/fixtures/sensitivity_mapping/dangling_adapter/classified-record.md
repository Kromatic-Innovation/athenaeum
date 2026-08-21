---
uid: fx0005classified
type: classified
sensitivity_class: classified
name: Fixture Classified Record
---

Synthetic fixture record for tests/test_sensitivity_lint.py. The paired test
config maps `storage.mapping.classified` to an adapter NAME that does not
exist in `storage.adapters` (nor is it a built-in) — this file exists to
prove the lint reports FINDING_DANGLING_ADAPTER, distinctly from
FINDING_MISSING_MAPPING, for a class mapped to a nonexistent surface.

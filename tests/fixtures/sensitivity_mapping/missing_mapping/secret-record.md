---
uid: fx0004secret
type: secret
sensitivity_class: secret
name: Fixture Secret Record
---

Synthetic fixture record for tests/test_sensitivity_lint.py. The paired test
config defines no `storage.mapping.secret` entry at all — this file exists
to prove the lint reports FINDING_MISSING_MAPPING for a class the corpus
carries but storage.mapping never names.

---
uid: fx0003person
type: person
name: Fixture Ordinary Person
---

Synthetic fixture record for tests/test_sensitivity_lint.py. Deliberately
carries NO `sensitivity_class:` field — an ordinary entity page with no
sensitivity classification at all. Proves the lint does not flag every
`type:` value in the corpus as a completeness gap (most entity classes never
have, and never need, a storage.mapping entry).

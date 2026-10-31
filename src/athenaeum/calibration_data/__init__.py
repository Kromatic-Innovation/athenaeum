# SPDX-License-Identifier: Apache-2.0
"""Packaged calibration records (issue athenaeum#2211).

Data only — no Python logic lives here. Each ``*.yaml`` sibling is one
calibration record: per-question-class tier/threshold data produced by the
calibration eval (``docs/design/attribution-verification.md`` §12). Loaded
by :func:`athenaeum.pii_attribution.load_calibration_record` via
``importlib.resources``, the same packaged-data-file pattern
``src/athenaeum/retention_packs/`` already uses (see that directory's
``pyproject.toml`` wheel-``include`` entry, which this directory's entry
mirrors).

Calibration records are data, never code: a record is replaced wholesale by
re-running the eval (``pytest tests/evals/test_attribution_eval.py -m
eval``), never by hand-editing a number in this file.
"""

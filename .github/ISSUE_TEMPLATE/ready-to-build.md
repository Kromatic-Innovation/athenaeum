---
name: Ready-to-build issue
about: Fully-spec'd issue that meets the hestia ready-to-build predicate. A human must apply the `hestia/ready-to-build` label after review.
title: ""
labels: ["hestia/planning"]
assignees: []
---

<!--
Canonical structure enforced by `scripts/gh-issue-safe.sh --rubric ready-to-build`.
See `.claude/skills/propose-issue/SKILL.md` for the predicate and label state machine.

This template files the issue under `hestia/planning`. A human reviews and
promotes to `hestia/ready-to-build` once all 6 conditions are verified.
-->

## Summary

<!-- One paragraph. Clear single outcome. -->

## Motivation

<!-- Why this matters now. Link prior context (retros, related issues, plan docs). -->

## Acceptance criteria

- [ ] <!-- At least one concrete, testable assertion. -->
- [ ]

## Plan

<!-- Specific files, modules, or areas the change is expected to touch. -->

## Out of scope

<!-- What this issue explicitly does NOT cover. -->

## Dependencies

<!-- Other issues, services, or work that must land first. Empty if none. -->

## Blocks on (hestia)

<!-- featureBuildBlocksOn categories: migrations, infra, secrets, breaking-api. Empty if none. -->

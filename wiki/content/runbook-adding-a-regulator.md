---
title: Adding a Regulator
type: runbook
sources: [CONTRIBUTING.md]
last-verified: 2026-09-10
---

## Purpose

Pointer to the mechanics of adding or fixing a regulator source. The actual step-by-step lives in [CONTRIBUTING.md](../../CONTRIBUTING.md), this page just orients you to it.

## How it works

Adding or fixing a regulator source is a three-part PR: one YAML entry under `config/regulators/<jurisdiction>.yaml`, one captured fixture under `tests/fixtures/sources/<regulator_id>/<label>.<ext>`, and no new test file (the existing contract test is parametrised over every enabled registry entry automatically). CONTRIBUTING.md covers all three in full, including the six adapter types and how to capture a fixture.

## Inputs

`config/jurisdictions.yaml` (valid jurisdiction ids), `config/regulators/_template.yaml` (commented template for all six adapter types), a live response from the source being added (captured via `scripts.capture_fixture`).

## Outputs and side effects

Registry validation runs on load (`collector.main.load_regulators()`) and fails loudly on a duplicate regulator id, a duplicate source label within a regulator, or an unknown jurisdiction. See CONTRIBUTING.md's "Registry validation" section for exactly what is and isn't checked.

## How to run or invoke

See CONTRIBUTING.md for the exact commands: `scripts.capture_fixture` to capture a fixture, `pytest tests/test_source_fixtures.py -k "<regulator_id>"` to confirm the adapter parses it, and `pytest tests/test_main.py -k load_regulators` to catch registry validation errors before opening a PR.

## How to test

Run the two pytest commands named in CONTRIBUTING.md's "Registry validation" and "Adding or fixing a regulator source" sections.

## Boundaries

Adding a genuinely new fetch strategy (not just a new source using an existing adapter type) means adding a new adapter file under `collector/adapters/`, not editing an existing one, see CONTRIBUTING.md's "Source adapter framework" section and [Source Adapter Framework](concept-source-adapter-framework.md).

## Related pages

[CONTRIBUTING.md](../../CONTRIBUTING.md), [Collector Adapters](component-collector-adapters.md), [Regulators Registry](config-regulators.md)

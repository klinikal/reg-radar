# reg-radar wiki

A per-component reference for reg-radar, written for an operator and for coding agents
equally. Every page follows the same template ([`_template.md`](content/_template.md)):
purpose, how it works, inputs, outputs and side effects, how to run it, how to test it,
and what it must never do. Prefer a page's linked source file over guessing; the page
names exactly which files it was written from.

## Concepts

- [Principles](content/concept-principles.md) — the five design principles v2 is built against
- [Canonical Document Format](content/concept-canonical-document-format.md) — the Markdown+frontmatter schema every item is materialised as
- [Taxonomy and Classification](content/concept-taxonomy-classification.md) — axes, provenance, precedence, and the classification store
- [Profiles and Activities](content/concept-profiles-activities.md) — how a saved filter expands to a jurisdiction/instrument set
- [Export and Sync Contract](content/concept-export-sync-contract.md) — the change-cursor primitive behind export, feeds, webhooks and digests
- [Source Adapter Framework](content/concept-source-adapter-framework.md) — the `fetch()` contract every regulator source implements

## Components

- [Collector Pipeline](content/component-collector-pipeline.md) — `collector/main.py` and the shared post-fetch pipeline
- [Collector Adapters](content/component-collector-adapters.md) — the adapter implementations under `collector/adapters/`
- [MCP Server](content/component-mcpserver.md) — `mcpserver/server.py` and `queries.py`, the 17 query/write tools
- [Web UI](content/component-webui.md) — `webui/app.py`, templates and static assets
- [Document](content/component-document.md) — `regradar/document.py`, rendering and parsing the canonical format
- [Taxonomy](content/component-taxonomy.md) — `regradar/taxonomy.py`, loading and validating `config/taxonomy.yaml`
- [Classification](content/component-classification.md) — `regradar/classification.py`, the write interface and effective-label precedence
- [Rules](content/component-rules.md) — `regradar/rules.py`, the weighted regex scoring engine
- [Profiles](content/component-profiles.md) — `regradar/profiles.py`, expansion and matching
- [Changes](content/component-changes.md) — `regradar/changes.py`, the cursor-based change stream
- [Webhooks](content/component-webhooks.md) — `regradar/webhooks.py`, HMAC-signed delivery with retry backoff
- [Delivery Channels](content/component-delivery.md) — `regradar/delivery/`, email/Telegram/webhook senders for digests
- [Providers](content/component-providers.md) — `regradar/providers/`, the pluggable LLM classifier backends
- [Registry](content/component-registry.md) — `regradar/registry.py`, loads and validates the regulators registry and `config/modules.yaml`

## CLI

- [CLI Entrypoint](content/cli-entrypoint.md) — `regradar/cli.py`, the subcommand registry
- [reg-radar render](content/cli-render.md) — `regradar/render.py`
- [reg-radar reclassify](content/cli-reclassify.md) — `regradar/reclassify.py`
- [reg-radar export](content/cli-exporter.md) — `regradar/exporter.py`
- [reg-radar digest](content/cli-digest.md) — `regradar/digest.py`
- [reg-radar classify](content/cli-classify.md) — `regradar/classify.py`
- [reg-radar import-dataset](content/cli-dataset.md) — `regradar/dataset.py`
- [reg-radar setup / modules](content/cli-modules.md) — `regradar/modules.py`

## Config

- [Taxonomy Config](content/config-taxonomy.md) — `config/taxonomy.yaml`
- [Jurisdictions Config](content/config-jurisdictions.md) — `config/jurisdictions.yaml`
- [Activities Config](content/config-activities.md) — `config/activities.yaml`
- [Rules Config](content/config-rules.md) — `config/rules.yaml`
- [LLM Prices Config](content/config-llm-prices.md) — `config/llm_prices.yaml`
- [Dataset Exclusions Config](content/config-dataset-exclusions.md) — `config/dataset_exclusions.yaml`
- [Profiles Config](content/config-profiles.md) — `config/profiles/`
- [Regulators Registry](content/config-regulators.md) — `config/regulators/`, one file per jurisdiction (38 files)
- [Modules Example Config](content/config-modules-example.md) — `config/modules.example.yaml`
- [Environment Variables](content/config-env-vars.md) — `.env.example`

## Scripts

- [Backfill Scripts](content/script-backfills.md) — one-off historic-archive pulls, one per source
- [Migration Scripts](content/script-migrations.md) — one-off v1-to-v2 data migrations, already run
- [Maintenance Scripts](content/script-maintenance.md) — rebuild/repair/reprocess utilities
- [Source Health](content/script-source-health.md) — `scripts/source_health.py`, the nightly contract check
- [Capture Fixture](content/script-capture-fixture.md) — `scripts/capture_fixture.py`, manual fixture recapture
- [Eval Classifier](content/script-eval-classifier.md) — `scripts/eval_classifier.py`, precision/recall against a gold sample
- [Check Wiki](content/script-check-wiki.md) — `scripts/check-wiki.py`, the coverage and consistency checker for this wiki

## Jobs

- [Source Health Nightly](content/job-source-health-nightly.md) — `.github/workflows/source-health-nightly.yml`
- [Publish Dataset](content/job-publish-dataset.md) — `.github/workflows/publish-dataset.yml`

## Runbooks

- [Upgrade and Migration](content/runbook-upgrade-and-migration.md) — upgrading from casp-radar and migrating v1 tag data
- [Backups and Recovery](content/runbook-backups-recovery.md) — litestream, restore, `originals/` coverage
- [Adding a Regulator](content/runbook-adding-a-regulator.md) — points to `CONTRIBUTING.md`

## Keeping this wiki current

Any change to `regradar/`, `collector/`, `mcpserver/`, `webui/` or `scripts/` updates the
matching wiki page in the same commit and bumps its `last-verified` date. A new component
gets a page from [`_template.md`](content/_template.md) before the change is considered
done. Run `python3 scripts/check-wiki.py` before committing; it fails if a source file has
no page, a page cites a source that no longer exists, a link doesn't resolve, or a page is
missing a required heading or `last-verified`.

The full workflow (reading, generating, updating, auditing, and how to delegate drafting to
a cheaper model without losing accuracy) is written up as the `reg-radar-wiki` skill at
[`SKILL.md`](SKILL.md).

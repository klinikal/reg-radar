---
title: Publish Dataset
type: job
sources: [.github/workflows/publish-dataset.yml]
last-verified: 2026-09-10
---

## Purpose

A scheduled GitHub Actions workflow that exports the full corpus and publishes it as a nightly GitHub release: a tarball of the markdown document tree plus `export.jsonl`, a `manifest.json`, a moving `latest` tag, and an immutable `dataset-YYYY-MM-DD` tag.

## How it works

Runs at 02:00 UTC daily (`0 2 * * *`), ahead of the collector's own default 06:00 UTC run so a nightly dataset never contains a half-finished run's items, plus `workflow_dispatch` for manual runs. It must run on a **self-hosted** runner (`runs-on: [self-hosted, reg-radar]`) alongside the collector's own compose stack, because it needs the collector's live SQLite database (the `reg_radar_data` named Docker volume), which a GitHub-hosted runner never has access to. The workflow's own header comment flags this as an assumption to verify before enabling the schedule for real, either point `runs-on` at a self-hosted runner on the collector's box, or restore a Litestream backup onto a hosted runner first. `docker compose run` also needs the collector host's own gitignored `.env` present in the checkout directory (`docker-compose.yml` declares `env_file: .env`).

Steps: checks out the repo, builds the collector image, exports markdown and jsonl in one `reg-radar export --format markdown --format jsonl` call (one shared cursor, so two separate export calls into the same `--out` would have the second read the first's already-advanced cursor and see nothing left), builds `manifest.json` and the tarball via `regradar.dataset.publish_dataset`, then publishes both a dated release (`dataset-$DATE`) and a rolling `latest` release via `gh release create`.

## Inputs

The live `reg_radar_data` Docker volume (collector's SQLite database), `config/dataset_exclusions.yaml` (excluded regulators, passed via `--exclude-regulator-file`), `config/taxonomy.yaml` and `config/jurisdictions.yaml` (for the taxonomy version stamped into the manifest), and the collector host's `.env`.

## Outputs and side effects

Writes `dist/export-md`, `dist/release/reg-radar-dataset-$DATE.tar.gz`, `dist/release/manifest.json`. Publishes two GitHub releases: `dataset-$DATE` (immutable, `--latest=false`) and `latest` (rolling pointer, deletes and recreates the existing `latest` release and tag first via `gh release delete latest` and `git push origin :refs/tags/latest`). Requires `contents: write` permission and `GH_TOKEN`.

## How to run or invoke

Runs automatically on the cron schedule on the self-hosted runner, or manually via `workflow_dispatch`. Locally, the equivalent export step is `reg-radar export --format markdown --format jsonl --out dist/export-md --exclude-regulator-file config/dataset_exclusions.yaml` run inside the `collector` container.

## How to test

Trigger via `workflow_dispatch` and confirm the `publish` job completes; check that `dist/release/manifest.json` and the tarball exist and that both the `dataset-$DATE` and `latest` GitHub releases were created.

## Boundaries

Must not run on a GitHub-hosted runner as currently written, it has no access to the collector's Docker volume. Must not run after the collector's own daily cycle starts, or the export could straddle a half-finished run.

## Related pages

[reg-radar import-dataset](cli-dataset.md)

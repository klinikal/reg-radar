---
title: Source Health Nightly
type: job
sources: [.github/workflows/source-health-nightly.yml]
last-verified: 2026-09-10
---

## Purpose

A scheduled GitHub Actions workflow that checks every enabled regulator source is still reachable, independent of the collector's own daily run.

## How it works

Runs on `ubuntu-latest` at 03:00 UTC daily (`0 3 * * *`), three hours clear of the collector's own default 06:00 UTC run, plus `workflow_dispatch` for manual runs. It brings up a `flaresolverr` service container (`ghcr.io/flaresolverr/flaresolverr:latest`, port 8191), the same image `docker-compose.yml` uses, so `cloudflare_protected` sources are checked live rather than silently skipped. It checks out the repo, sets up Python 3.11, installs `collector/requirements.txt`, then runs `python -m scripts.source_health`.

## Inputs

`REGULATORS_PATH=config/regulators`, `FLARESOLVERR_URL=http://localhost:8191`, `GH_TOKEN` (the workflow's own `github.token`).

## Outputs and side effects

The job has `issues: write` and `contents: read` permissions, implying `scripts.source_health` can open or update GitHub issues when a source fails. [UNVERIFIED] the exact issue-creation behaviour, that lives in `scripts/source_health.py` and isn't covered by this workflow file.

## How to run or invoke

Runs automatically on the cron schedule, or manually via `workflow_dispatch` in the GitHub Actions UI. Locally, the equivalent is `REGULATORS_PATH=config/regulators python -m scripts.source_health` with a FlareSolverr instance reachable at `FLARESOLVERR_URL`.

## How to test

Trigger the workflow manually with `workflow_dispatch` and confirm the `check-sources` job completes; or run `python -m scripts.source_health` locally against a running FlareSolverr container.

## Boundaries

Must not run against the same host/time window as a real collector run without separation, hence the 03:00 UTC schedule offset from the collector's 06:00 UTC default.

## Related pages

[Source Health](script-source-health.md)

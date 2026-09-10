---
title: Backups and Recovery
type: runbook
sources: [README.md]
last-verified: 2026-09-10
---

## Purpose

How reg-radar's database is backed up and how to recover it after corruption or data loss.

## How it works

The live database lives in a Docker-managed named volume (`reg_radar_data`), not a bind-mounted host folder, because SQLite's WAL mode needs file-locking guarantees that Docker Desktop's macOS bind-mount bridge doesn't fully honour, which caused a real corruption incident. A `litestream` service continuously replicates the database to `./backups/reg_radar` on the host disk, so a corruption or data-loss event is a restore, not a salvage job.

The collector also runs a full `PRAGMA integrity_check` at the start of every collection cycle, so corruption is caught immediately and surfaced loudly via `get_health_tool`'s `db_integrity` field (`"ok"`, `"corrupted"`, or `"unknown"` if no run has completed yet), rather than discovered later as a failed search.

**`originals/` is not covered by Litestream.** Litestream only replicates the SQLite file. The original artefacts (fetched HTML/PDF) live on the same data volume under `originals/` and need their own host-level backup, a periodic `docker cp`/volume snapshot, or whatever backup tool is already run against the host.

## Inputs

The live `reg_radar_data` Docker volume, the `litestream` service configuration.

## Outputs and side effects

A continuous replica of the SQLite database at `./backups/reg_radar` on the host. No replica of the `originals/` artefacts directory, that is out of scope for Litestream and must be backed up separately.

## How to run or invoke

The full restore procedure and further detail live in [Backups & recovery in the operator skill](../../skills/reg-radar-operator/SKILL.md#backups--recovery), the README does not restate the restore steps here, only the replication design.

## How to test

Check `get_health_tool`'s `db_integrity` field is `"ok"` after a collection cycle. To validate a restore actually works, follow the restore procedure in the operator skill against a test copy.

## Boundaries

Must not treat Litestream replication as covering `originals/`, it does not. Must not rely on the Docker Desktop bind-mount path for the live database, that is the cause of the documented corruption incident that led to the named-volume design.

## Related pages

[README.md](../../README.md)

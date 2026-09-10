---
title: Upgrade and Migration
type: runbook
sources: [README.md]
last-verified: 2026-09-10
---

## Purpose

How to upgrade an existing casp-radar install to reg-radar, and how to bring a v1 database's labels, classifications and document bodies up to the v2 schema.

## How it works

The project was renamed from casp-radar to reg-radar (same code, same data, new name and new default volume/database names; the codebase briefly used the interim name `regradar` during v2 development). On a fresh install, none of this applies.

There are two upgrade options. **Option A** keeps running on the old volume by setting `DATA_VOLUME_NAME=casp_radar_data` in `.env` and restarting, nothing else changes. **Option B** migrates data into a new volume named `reg_radar_data` and moves the Litestream backup directory to match; the old volume is only deleted at the end, after the new one is confirmed working.

Once the volume is renamed, a separate step migrates the database *contents*: v1 labels, classifications and document bodies to the v2 schema, run once after the stack is up on the new volume.

## Inputs

`.env` (`DATA_VOLUME_NAME` override for Option A), the existing `casp_radar_data` Docker volume, the existing `./backups/casp_radar` Litestream directory.

## Outputs and side effects

Option B creates a new `reg_radar_data` Docker volume, moves `./backups/casp_radar` to `./backups/reg_radar`, and (only after verification) deletes the old `casp_radar_data` volume. The content migration scripts rewrite the `regulators` table's `jurisdiction` column and reclassify existing items into v2 labels.

## How to run or invoke

**Option A** (stay on old volume):
```bash
echo "DATA_VOLUME_NAME=casp_radar_data" >> .env
docker compose up -d
```
Remove that line once migrated with Option B so future upgrades use the new default.

**Option B** (migrate to new volume), in order, old volume deleted only at the end:
```bash
# 1. If Option A was used, remove the override first
docker compose down
sed -i.bak '/^DATA_VOLUME_NAME=/d' .env   # macOS leaves a .env.bak to delete after checking .env

# 2. Copy data into the new volume
docker volume create reg_radar_data
docker run --rm -v casp_radar_data:/from -v reg_radar_data:/to alpine sh -c "cp -a /from/. /to/"

# 3. Move the Litestream backup directory to match
mv ./backups/casp_radar ./backups/reg_radar

# 4. Start the stack and confirm count_items_tool matches the pre-migration count
docker compose up -d

# 5. Only once step 4 checks out, delete the old volume
docker volume rm casp_radar_data
```

**v1 data migration** (content, after the stack is up on the new volume), in order:
```bash
# Refreshes regulators.jurisdiction on upsert; or run scripts/migrate_regulator_labels.py --db-path <path> directly
# (run the collector once)
reg-radar reclassify --axis document_type
reg-radar reclassify --axis instrument --by rule
reg-radar reclassify --axis document_type --by rule
python scripts/migrate_content_tags.py
python scripts/reprocess_bodies.py --from-raw-content
```

## How to test

For the volume migration: after step 4, `count_items_tool` must return the same number of items as before the migration. For the data migration: full detail and `--dry-run` notes are in the README's Classification section.

## Boundaries

Deleting the old `casp_radar_data` volume before confirming `count_items_tool` matches leaves nothing to fall back to, Litestream would already have replicated the new (possibly empty) database over `./backups/reg_radar`. Must not skip removing the `DATA_VOLUME_NAME` override before Option B, step 4 would otherwise still resolve to the old volume name and start against a brand-new empty one.

## Related pages

[Migration Scripts](script-migrations.md), [Taxonomy and Classification](concept-taxonomy-classification.md)

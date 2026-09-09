# Published dataset: nightly corpus release so new users skip backfills

**Size:** S  **Depends on:** #4 canonical document format, #8 export, changes cursor, outbound feeds and webhooks  **Design doc:** docs/design/regradar-architecture.md §8 (row 10)

## Why

A new RegRadar user starts with an empty database and has to run the collector from cold, then decide whether to run the historic backfill scripts documented in the README (ESMA's ~7,260 documents, FCA's ~9,700, MFSA's ~65 categories), each slow and FlareSolverr-throttled by design. A nightly published dataset lets a new user download one tarball, load it, and have the collector pick up only what is genuinely new from then on, skipping the historic crawl unless they specifically want to re-verify it.

## Scope

- A GitHub Actions workflow, scheduled nightly, that runs `regradar export` (from #8) over the full corpus in both `markdown` and `jsonl` formats, no originals by default.
- Packaging: a versioned tarball plus `manifest.json` (schema version, taxonomy version, item count, per-regulator counts, `generated_at`, sha256 of the tarball).
- Publishing: a GitHub release, with a rolling `latest` tag moved forward each run and a dated tag (e.g. `dataset-2026-09-09`) that is never moved, so users can pin to a known snapshot.
- `regradar import-dataset <url|path>`: downloads or reads the tarball, verifies its sha256 against the manifest, loads it into an empty database, and sets the sync cursor (#8's `updated_at|id` shape) to the manifest's `generated_at`, so the next collector run only fetches items newer than that.
- A `NOTICE` file at the repository root, one row per regulator, recording what is known about reuse terms for that regulator's published content.
- An exclusion list so regulators with unclear terms are dropped from the published dataset without affecting the live collector or a local `regradar export`.
- README: new "Published dataset" section covering cadence, `import-dataset`, and a pointer to `NOTICE`.

## Non-goals

- Any change to what the collector or a local `regradar export` do for a single user.
- Resolving licensing for every regulator now; this issue ships the mechanism and a first-pass inventory, not a legal sign-off.
- Hosting the dataset anywhere other than GitHub Releases.
- Incremental/delta releases; each nightly release is a full snapshot.

## Design notes

- `.github/workflows/publish-dataset.yml`: nightly cron, builds the existing Docker images, runs `regradar export --format markdown --format jsonl --out dist/export --exclude-regulator-file config/dataset_exclusions.yaml`, builds the manifest, tars, computes sha256, creates/moves the GitHub release via the GitHub CLI.
- `regradar/cli.py` (created in #4): add `import-dataset` as a subcommand alongside `export`. `manifest()` builder and `import_dataset(source)` loader live in `regradar/dataset.py`, imported by the CLI.
- `config/dataset_exclusions.yaml`: flat list of regulator ids excluded pending licence clarification.
- `NOTICE`: one row per regulator, e.g. `esma: EU institution, reuse permitted with attribution (Decision 2011/833/EU)`, `fca: UK OGL v3.0`, `mfsa: terms unclear, excluded`. A starting inventory, not a legal sign-off; mark unresolved rows as unresolved rather than guessing.

Manifest shape:

```json
{
  "regradar_schema": 1,
  "taxonomy_version": 3,
  "generated_at": "2026-09-09T02:14:00Z",
  "item_count": 41823,
  "per_regulator_counts": {"esma": 7260, "fca": 9743},
  "excluded_regulators": ["mfsa"],
  "sha256": "…",
  "tarball": "regradar-dataset-2026-09-09.tar.gz"
}
```

`import-dataset`: refuses a non-empty database unless `--force`; verifies sha256 before writing anything; loads items and classifications directly rather than re-parsing Markdown through the collector's fetch path; sets the stored sync cursor to `generated_at`; prints a summary of items loaded and excluded regulators, noting they need a manual backfill if wanted.

## Acceptance criteria

- [ ] The workflow runs on schedule, produces a tarball and `manifest.json`, and publishes a release with both a moving `latest` tag and an immutable dated tag.
- [ ] The manifest's sha256 matches the tarball; `item_count` and `per_regulator_counts` match the exported corpus.
- [ ] Regulators in `config/dataset_exclusions.yaml` are absent from the tarball and listed under `excluded_regulators`.
- [ ] `regradar import-dataset <path>` loads a local tarball into an empty database, rejects a tampered tarball on checksum mismatch, and refuses a non-empty database without `--force`.
- [ ] After import, a `get_changes_tool` call from the stored cursor returns nothing until the next collector run adds newer items, and that run only inserts items published after `generated_at` where source pagination allows detecting that.
- [ ] `NOTICE` has at least one row per currently enabled regulator in `config/regulators.yaml`, each marked with a known licence, "unclear", or "excluded".
- [ ] README documents cadence, the download URL pattern, `import-dataset`, and links to `NOTICE`.

## Open questions

- Most regulator content is public sector information, but terms genuinely vary (UK Open Government Licence, EU Decision 2011/833/EU, several regulators with no stated reuse policy at all). Recommend the `NOTICE` file plus the exclusion list as the mechanism, defaulting new regulators to excluded until their terms are recorded, but this needs a real legal read, not an engineering judgement call.
- Should the published dataset include `label_provenance` and human corrections, or only effective `labels`? Provenance is more useful downstream but roughly doubles frontmatter size; leaning towards including it since storage is cheap per design doc §3.3.
- Retention: how many dated releases to keep before pruning. Not addressed here; decide once real tarball sizes are known.

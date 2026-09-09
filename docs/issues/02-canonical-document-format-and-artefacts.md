# Canonical document format and original artefact storage

**Size:** M  **Depends on:** #3 rename  **Design doc:** docs/design/regradar-architecture.md §3

## Why

Today an item is a row in SQLite holding extracted plain text, and nothing else survives collection. There is no file a document management system can ingest, and if the extractor improves or a legal team wants the actual PDF, the only option is to re-fetch from the regulator. This issue defines the one file format every downstream adapter reads, and keeps the original artefact so nothing has to be fetched twice.

## Scope

- A new top-level `regradar/` package (design doc §6a) installed into all three Docker images, with `regradar/cli.py` as the `regradar` console entrypoint (declared in a `pyproject.toml`). This issue adds the `render` subcommand; later issues add `reclassify`, `export`, `classify`, `digest` and `import-dataset` here rather than as separate scripts.
- A `regradar.document` module that renders any item to the §3.2 frontmatter plus Markdown body, and parses it back. Round trip must be lossless for every frontmatter key.
- Body conversion to Markdown at collection time, stored in a new `items.body_md` column. HTML to Markdown preserving headings, paragraphs, lists, links and simple tables. PDF text goes through the same paragraph normalisation as today plus heading detection where pypdf exposes font size. Store the conversion library name and version in `items.extractor` so a later reprocess can target only old rows.
- Original artefact storage per §3.3: `originals/<id2>/<id>.<ext>` on the data volume, an `item_artefacts` table, and a `MAX_ARTEFACT_BYTES` cap defaulting to the existing PDF cap. RSS items with no fetched body store nothing.
- `items.updated_at`, set on insert and on any change to body, labels or artefact. This column is the basis for the sync cursor in #8.
- `regradar render <item_id>` and `regradar render --all --out <dir>` commands that write the §3.1 layout. The exporter proper, with profiles and cursors, is #8; this issue provides the per-item rendering it will call.
- `scripts/reprocess_bodies.py` that re-runs Markdown conversion from stored artefacts for items whose `extractor` is older than the current one. `--dry-run` and `--limit` as with the existing scripts.
- A JSON Schema for the frontmatter at `docs/schema/document-v1.json`, and a test that every rendered file validates against it.
- Update `get_item_tool` and `/api/items/{id}` to return `body_md` alongside `raw_content`, and to include the frontmatter fields as a `document` object.

## Non-goals

- Profiles, filtering and the change cursor endpoint belong to #7 and #8.
- Labels and provenance are written by #5 and #6. This issue renders whatever `items.labels` contains, which will be empty until #5 lands; the renderer must handle that without error.
- No semantic chunking changes. `item_chunks` keeps working from `raw_content` until it is migrated to `body_md` in a follow-on.
- No change to the item id scheme.

## Design notes

Storage additions in `collector/storage.py`, using the existing `_ensure_column` pattern for the new columns:

```python
class ItemArtefactRow(Base):
    __tablename__ = "item_artefacts"
    item_id = Column(String, primary_key=True)
    path = Column(String, nullable=False)     # relative to ARTEFACTS_DIR
    mime = Column(String, nullable=False)
    sha256 = Column(String, nullable=False)
    bytes = Column(Integer, nullable=False)
    fetched_at = Column(DateTime, nullable=False)
```

New env vars: `ARTEFACTS_DIR` (default `/app/data/originals`), `MAX_ARTEFACT_BYTES`, `STORE_ARTEFACTS` (default true, so a constrained install can opt out).

The renderer signature: `render_item(item: Item, artefact: ItemArtefactRow | None, labels: dict, provenance: list[dict]) -> str`. Path helper: `document_path(item) -> Path` implementing the §3.1 slug rules, with a test covering non-ASCII titles (German umlauts, French accents, Greek and Bulgarian script), empty titles and a missing `published_at`.

For the body, evaluate `markdownify` and `html2text` against five fixtures (ESMA news, FCA consultation page, BaFin press release, a scraped PDF, an RSS-only snippet) and pick one; record the choice and the fixtures in the PR. Whichever wins, wrap it so the library can be swapped without touching callers.

`fetcher.py` currently discards the fetched HTML after parsing and reads PDFs into memory only for text extraction. Both paths need to hand the raw bytes and content type through to storage. Do this by adding `raw_bytes` and `mime` to the `Item` dataclass, populated by `_parse_scrape` when `extract_pdf_text` or a body fetch happens, and consumed by `insert_item`. Adapters in #10 will later carry the same fields on `RawDocument`.

Litestream replicates the SQLite file only. Add a note to the backups section of the README that `originals/` is not covered and should be included in whatever host-level backup the operator runs, and add the directory to the restore procedure in the operator skill.

## Acceptance criteria

- [ ] Every item in a freshly collected database renders to a file that validates against `docs/schema/document-v1.json`.
- [ ] `parse(render(item)) == item` for all frontmatter fields, tested on at least 20 real items across RSS, scrape and page_watch sources.
- [ ] A scraped PDF item has a row in `item_artefacts`, the file exists on disk, and its sha256 matches.
- [ ] `regradar render --all --out /tmp/x` produces the §3.1 directory layout; running it twice does not rewrite unchanged files (check mtimes).
- [ ] Rendered files open in Obsidian with frontmatter shown as properties and the body readable, verified manually on the five fixture documents and screenshots attached to the PR.
- [ ] `STORE_ARTEFACTS=false` collects normally and writes no artefacts.
- [ ] `scripts/reprocess_bodies.py --dry-run` on the existing dataset reports counts without writing.
- [ ] `regradar --help` works inside each of the three containers.
- [ ] Full test suite passes.

## Open questions

- Whether `body_md` should replace `raw_content` in the FTS index or sit alongside it. Recommendation: replace, since Markdown syntax adds little noise and one index is simpler, but confirm search quality on a sample before switching.
- Whether to shard `originals/` by item id prefix (as specified) or by regulator. Item id is simpler and content-addressed; regulator is more browsable. Specified as item id; raise if there is a reason to change.

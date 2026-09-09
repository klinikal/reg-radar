# RegRadar architecture: contracts for v2

Status: draft for discussion. Owner: klinikal. Last updated: 2026-09-09.

This document fixes the three contracts that every v2 issue builds against: the canonical document format, the taxonomy and classification model, and the profile and export interfaces. Issues reference this doc rather than restating it. If an implementation needs to deviate, change this doc first in the same PR.

## 1. What v2 is

casp-radar v1 is a crypto-specific collector: 44 regulators, flat tags dominated by `mica` and `casp`, plain-text storage, one MCP server and a small web UI. v2, renamed RegRadar, becomes a sector-neutral regulatory feed with:

- a stable, file-based document format any downstream tool can ingest;
- an objective, config-driven taxonomy with provenance on every label;
- user profiles that filter the whole corpus down to what a given regulated entity needs;
- a source framework where adding a regulator is one config file plus one test;
- delivery primitives (export, sync cursor, outbound feeds, digests) rather than a UI-first product.

Non-goals for v2: multi-tenant hosting, a Postgres backend, a rich frontend, any built-in relevance scoring beyond the taxonomy and profile filters. The "bring your own agent" stance stays, with one change: classification is now a first-class, optional, pluggable step (section 4).

## 2. Principles

1. **Config over code.** Vocabularies, hierarchies, rules and activity mappings live in YAML under `config/`. Adding a value, a jurisdiction or an activity must never need a code change or a migration.
2. **Objective labels, interpretive mappings.** The classifier assigns only things that are matters of fact: which regulator, which jurisdiction, which legal instrument, what kind of document. "What kind of business cares about this" is a mapping in config that people can argue about in pull requests, never a label the classifier writes.
3. **Provenance on everything.** Every label records who assigned it, how confident they were, and which taxonomy version was in force. Early classifications will be wrong. Reclassifying must be cheap and must never destroy human corrections.
4. **Interfaces before implementations.** The write interface for classifications exists before any classifier. The export contract exists before any adapter. The source adapter interface exists before the next regulator is added.
5. **Nothing silently disappears.** Unclassified items are visible by default in every profile. A stalled source is an alert, not an empty list.

## 3. Canonical document format

Every item is materialised as one Markdown file with YAML frontmatter, plus the original artefact where one was fetched. This is the ingestion contract for Obsidian, Notion, SharePoint, a vector store, or anything else. SQLite remains the query store; the files are a projection of it, regenerated on demand by the exporter (section 6).

### 3.1 Layout

```
export/
  <jurisdiction>/<regulator_id>/<yyyy>/<yyyy-mm-dd>-<slug>-<id8>.md
originals/
  <id2>/<id>.<ext>          # content-addressed by item id, sharded on first two hex chars
```

`id8` is the first 8 hex characters of the item id. `slug` is the title, lower-cased, ASCII-folded, non-alphanumerics collapsed to single hyphens, truncated to 80 characters. Items with no `published_at` use `fetched_at` for the path and set `date_inferred: true`.

### 3.2 Frontmatter (schema version 1)

```yaml
---
regradar_schema: 1
id: 9f2c...                      # sha256, unchanged from v1 make_item_id
regulator: fca
regulator_name: Financial Conduct Authority
jurisdiction: gb
jurisdiction_ancestors: []       # e.g. [eu] for fr and for no; [us] for us-ny
title: "CP26/12: Proposed changes to the client assets sourcebook"
published_at: 2026-08-14
fetched_at: 2026-08-15T06:02:11Z
updated_at: 2026-08-20T06:00:03Z # bumps on any label or content change; drives sync
date_inferred: false
language: en
source_url: https://www.fca.org.uk/publications/consultation-papers/cp26-12
source_label: policy_guidance
source_type: scrape
labels:                          # effective labels after precedence (section 4.4)
  document_type: consultation
  instrument: [cass, mifid2]
label_provenance:                # one entry per effective value
  - axis: document_type
    value: consultation
    by: source_default
    confidence: 1.0
    taxonomy_version: 3
  - axis: instrument
    value: cass
    by: llm:anthropic/claude-sonnet-5
    confidence: 0.91
    taxonomy_version: 3
content_hash: 4b1e...            # sha256 of the body text below
original:
  path: originals/9f/9f2c....pdf
  mime: application/pdf
  sha256: 77aa...
  bytes: 418233
---
```

Rules:

- Keys are stable. New keys may be added under schema version 1; removing or renaming a key bumps `regradar_schema`.
- `labels` holds effective values only. Axis names and values come from `config/taxonomy.yaml` (section 4.1). Single-valued axes hold a scalar, multi-valued axes hold a list, absent axes are omitted.
- Activities (section 5) are never written to frontmatter. They are derived at filter time from `instrument` via `config/activities.yaml`.
- The body below the frontmatter is the cleaned document text as Markdown: headings preserved where the source had them, paragraphs separated by blank lines, tables as pipe tables where extraction allows, otherwise plain paragraphs. No HTML.
- Where the item is an RSS snippet with no fetched body, the body is the snippet followed by a single line `> Full text not collected. See source_url.`

### 3.3 Original artefacts

The collector stores the original response for every item that has a body: HTML as fetched (after FlareSolverr where used), PDF bytes as fetched. Stored under `originals/` on disk, referenced from a new `item_artefacts` table (item_id, path, mime, sha256, bytes, fetched_at). Size caps stay as they are (`MAX_PDF_DOWNLOAD_BYTES`). Originals are excluded from the published dataset by default and included with a flag.

Rationale: v1 keeps only extracted text, so a better extractor, a new chunker, or a legal team wanting the actual PDF all require re-fetching from the regulator. Storage is cheap; regulators' goodwill is not.

## 4. Taxonomy and classification

### 4.1 `config/taxonomy.yaml`

Declares the axes and their vocabularies. The code knows nothing about specific axes except `jurisdiction`, which has hierarchy semantics (4.2).

```yaml
version: 3                        # bump on any change to values; recorded on every label
axes:
  - id: document_type
    cardinality: one
    values:
      - id: consultation
        name: Consultation paper
      - id: final_guidance
        name: Final guidance, policy statement or Q&A
      - id: technical_standard
        name: Technical standard (RTS, ITS, delegated act)
      - id: legislation
        name: Primary or secondary legislation
      - id: enforcement
        name: Enforcement action, fine or decision notice
      - id: warning
        name: Consumer or firm warning
      - id: speech
        name: Speech or statement
      - id: press_release
        name: Press release or news
      - id: register_update
        name: Register or authorisation change
      - id: page_change
        name: Watched page changed
  - id: instrument
    cardinality: many
    values:
      - id: mica
        name: Markets in Crypto-Assets Regulation
        aliases: ["MiCA", "MiCAR", "Regulation (EU) 2023/1114"]
        jurisdiction: eu
      - id: mifid2
        name: MiFID II / MiFIR
        aliases: ["MiFID II", "MiFID 2", "MiFIR", "Directive 2014/65/EU"]
        jurisdiction: eu
      - id: mar
        name: Market Abuse Regulation
        aliases: ["MAR", "Regulation (EU) 596/2014"]
        jurisdiction: eu
      - id: dora
        name: Digital Operational Resilience Act
        aliases: ["DORA", "Regulation (EU) 2022/2554"]
        jurisdiction: eu
      - id: amld
        name: EU AML framework (AMLD4-6, AMLR, AMLA Regulation)
        aliases: ["AMLD", "AMLR", "anti-money laundering directive"]
        jurisdiction: eu
      - id: psd2
        name: Payment Services Directive 2 / PSD3 / PSR
        jurisdiction: eu
      - id: crr_crd
        name: Capital Requirements Regulation and Directive
        jurisdiction: eu
      - id: uk_fsma
        name: Financial Services and Markets Act 2000
        jurisdiction: gb
      - id: uk_consumer_duty
        name: FCA Consumer Duty (PRIN 2A)
        jurisdiction: gb
      - id: uk_mlr
        name: Money Laundering Regulations 2017
        jurisdiction: gb
      # ... grows by PR, no code change
```

`aliases` feed the rules engine (4.3) and the LLM prompt. `jurisdiction` on an instrument is informational and used as a sanity check: an item from a UK regulator labelled `mica` is not wrong (the FCA writes about MiCA), so it is a warning in the classifier's log, never a rejection.

A `topic` axis (cross-cutting themes that are not a legal instrument) is deliberately absent from v2. If one is needed later, adding it is a config change and the code already handles arbitrary axes.

### 4.2 `config/jurisdictions.yaml`

```yaml
- id: eu
  name: European Union
  type: supranational
- id: fr
  name: France
  parents: [eu]
- id: no
  name: Norway
  parents: [eu]
  note: EEA-EFTA state; EU financial services acquis applies once incorporated into the EEA Agreement.
- id: gb
  name: United Kingdom
- id: us
  name: United States
- id: us-ny
  name: New York
  parents: [us]
```

`parents` is a list so the graph can be a DAG. Ancestors are the transitive closure. A profile selecting `fr` matches items whose jurisdiction is `fr` or any ancestor of `fr`. Regulators keep their `jurisdiction` field; it must be an id in this file and the collector fails loudly at startup if it is not. The existing `regulators.yaml` uses ISO alpha-2 upper-case; migration lower-cases.

### 4.3 `config/rules.yaml`

Replaces `alerts.yaml`. Deterministic, weighted, regex-capable, scoped to an axis and value.

```yaml
- axis: instrument
  value: mica
  match:
    - pattern: "\\bMiCA(R)?\\b"
      weight: 0.9
    - pattern: "crypto-asset service provider"
      weight: 0.6
      flags: i
  fields: [title, body]
  title_multiplier: 1.5
- axis: document_type
  value: consultation
  match:
    - pattern: "^(CP|Consultation Paper)\\s*\\d"
      weight: 0.95
  fields: [title]
```

Scores sum per (axis, value), capped at 1.0. A rule label is written when the score reaches `min_confidence` (default 0.5, set at the top of the file). Alias lists in `taxonomy.yaml` are compiled into implicit rules at weight 0.7 so the common case needs no rule at all.

Source defaults remain in `regulators.yaml`, renamed from `tags` to `labels`:

```yaml
sources:
  - type: scrape
    url: https://www.mfsa.mt/publications/crypto-assets-circulars/
    label: crypto_circulars
    labels:
      document_type: final_guidance
      instrument: [mica]
```

Source defaults carry `by: source_default`, confidence 1.0 for `document_type` on single-purpose feeds and 0.6 for instruments, because a feed named "crypto circulars" is strong evidence but not proof.

### 4.4 Classification storage and precedence

New table `classifications`:

| column | notes |
|---|---|
| item_id | FK items.id |
| axis | from taxonomy |
| value | from taxonomy; unknown values are rejected on write |
| by | `source_default`, `rule`, `llm:<provider>/<model>`, `human:<name>`, `import` |
| confidence | 0.0 to 1.0 |
| taxonomy_version | integer from taxonomy.yaml at write time |
| classifier_id | free text, e.g. rules file hash or skill version |
| created_at | |
| superseded_at | null while live |

Effective labels per axis, computed on every write and on `reclassify`, materialised into `items.labels` (JSON) and `items.updated_at`:

1. Any live `human:*` row wins for that axis outright. For multi-valued axes, human rows define the full set.
2. Otherwise, take all live non-human rows with `confidence >= threshold` (default 0.5, per axis override in taxonomy.yaml). Single-valued axes take the highest confidence, ties broken by `llm > rule > source_default`. Multi-valued axes take the union.
3. Otherwise the axis is unset. Unset is a real state: profiles include unset items by default (section 5).

`reclassify --axis instrument --by rule` marks matching non-human rows superseded and rewrites them from current config. `--dry-run` reports what would change. Human rows are never touched by any automated path.

### 4.5 Write interface

One function, `write_classification(item_id, axis, value, by, confidence, classifier_id)`, exposed three ways: the Python module, an HTTP endpoint `POST /api/items/{id}/classifications`, and an MCP tool `classify_item_tool`. All validate against the current taxonomy. A companion `get_unclassified_tool(axis, limit, offset)` returns items with no live row on that axis, oldest first, so any agent can work through the backlog.

### 4.6 LLM classifier

v2 ships the LLM classifier as a skill, not a service: `skills/regradar-classifier/SKILL.md` instructs any MCP-connected agent to pull unclassified items, read `taxonomy.yaml`, classify, and write back via `classify_item_tool` with a confidence and `by: llm:<provider>/<model>`. No keys in the repo, no provider lock-in, works with a subscription-backed agent or any API-backed one.

A batch runner (`regradar classify --provider anthropic|openai|ollama`) that does the same unattended is a follow-on issue, not v2 core. The skill and the runner share the write interface, so nothing downstream cares which one populated a label.

### 4.7 Migration from v1 tags

- `source_tags` values `warnings`, `press_releases`, `news`, `publications`, `page_watch` map to `document_type` source defaults: `warning`, `press_release`, `press_release`, unset, `page_change`. `publications` is too broad to map; those sources get no document_type default and rely on rules.
- `source_tags` values `mica`, `casp`, `aml`, `dora`, `art`, `emt`, `crypto` become `instrument` source defaults only where the source is single-purpose (MFSA crypto circulars, ESMA MiCA hub). Elsewhere they are dropped. The v1 README already notes they were stamped indiscriminately.
- `content_tags` from `alerts.yaml` become `rules.yaml` entries; existing per-item `content_tags` are imported as `by: import`, confidence 0.5, so they survive but rank below anything better.
- `source_tags` and `content_tags` columns are kept for one release, read-only, then dropped.

## 5. Profiles and activities

### 5.1 `config/activities.yaml`

The interpretive layer, in config, versioned by PR.

```yaml
- id: equities_dealing
  name: Dealing and broking in equities
  instruments: [mifid2, mar, csdr, emir, sftr, uk_fsma, uk_consumer_duty]
- id: casp
  name: Crypto-asset service provider
  instruments: [mica, amld, dora, travel_rule, uk_mlr, uk_fin_prom]
- id: emi
  name: Electronic money or payment institution
  instruments: [psd2, emd2, amld, dora, uk_psr]
- id: bank
  name: Credit institution
  instruments: [crr_crd, brrd, dgsd, amld, dora]
```

### 5.2 Profile file

```yaml
name: uk-equities-dealer
jurisdictions: [gb]             # "eu/*" selects the EU and every jurisdiction beneath it
activities: [equities_dealing]
instruments: []                  # additional explicit instruments
exclude_instruments: [mica]      # hard exclude, applied last
document_types: []               # empty means all
regulators: []                   # empty means all in matched jurisdictions
languages: [en]
include_unclassified: true       # default true; see principle 5
```

### 5.3 Expansion

1. Jurisdiction set = union of each selected jurisdiction and its ancestors. An entry written as `<id>/*` also includes every descendant of `<id>`, so `eu/*` is ESMA-level output plus every member state regulator, while plain `eu` is ESMA-level output only. A French firm selects `fr`; a pan-EU firm selects `eu/*`.
2. Instrument set = union of instruments from selected activities, plus explicit `instruments`, minus `exclude_instruments`.
3. An item matches if its jurisdiction is in the jurisdiction set, and (its instrument labels intersect the instrument set, or it has no instrument label and `include_unclassified` is true), and its document_type is in `document_types` when that list is non-empty, and the same for `regulators` and `languages`.

Profiles apply identically in the web UI (a profile picker), every MCP query tool (a `profile` parameter), the JSON API, and the exporter. Shipped templates live in `config/profiles/`. A user's own profile lives wherever they like and is passed by path.

## 6. Export and sync contract

The primitive every adapter builds on is "everything that changed since cursor X".

- Cursor: `updated_at` ISO timestamp plus item id as tiebreak, opaque to callers, returned by every call.
- `GET /api/changes?since=<cursor>&profile=<name>&limit=` returns items (frontmatter fields as JSON, body optional via `include_body=true`) and the next cursor. The MCP tool `get_changes_tool` mirrors it and replaces `get_items_since_tool`.
- `regradar export --profile <p> --format markdown|jsonl --out <dir> [--since <cursor>] [--with-originals]` writes the section 3 layout. Idempotent: unchanged items are not rewritten, so a directory can be pointed at directly by Obsidian or synced by any file tool.
- Outbound feeds: `GET /feeds/<profile>.atom` for anything that reads RSS. Webhook delivery is a subscriber to the same change stream.

The Obsidian adapter is therefore the exporter plus a folder inside a vault. It is out of scope to build a plugin; it is in scope to make the exporter output open cleanly in Obsidian with working frontmatter properties.

## 6a. Package layout

Shared v2 code lives in a new top-level `regradar/` Python package (`document`, `taxonomy`, `classification`, `profiles`, `exporter`, `cli`), installed into all three Docker images alongside the existing `collector`, `mcpserver` and `webui` packages. The `regradar` console command is `regradar/cli.py`, created in issue #4 (row 2); later issues add subcommands to it rather than adding scripts.

## 7. Source framework

Each `sources[].type` maps to a registered adapter class with one method: `fetch(source_cfg, ctx) -> list[RawDocument]`, where `RawDocument` carries url, title, published_at, body bytes, mime and the source's `labels`. v1 types `rss`, `scrape`, `page_watch` become adapters; `sitemap`, `json_api` and `browser` (Playwright) are the first additions. Everything after fetch (extraction, hashing, classification, storage, artefact write) is shared and adapter-agnostic.

Every source gets an HTML or feed fixture under `tests/fixtures/sources/<regulator>/<label>.html` and a contract test asserting the adapter yields at least one item with a title, url and date from that fixture. A nightly workflow runs the same test against the live URL and opens or updates a GitHub issue per source on failure. The registry splits into `config/regulators/<jurisdiction>.yaml`, loaded as a directory.

## 8. Build order

| # | GitHub | Issue | Depends on | Size |
|---|---|---|---|---|
| 1 | #3 | Rename to RegRadar | none | S |
| 2 | #4 | Canonical document format and original artefact storage | 1 | M |
| 3 | #5 | Taxonomy, jurisdictions and classification store with provenance | 1 | M |
| 4 | #6 | Rules engine, classification write interface and LLM classifier skill | 3 | M |
| 5 | #7 | Profiles and activities | 3 | S to M |
| 6 | #8 | Export, changes cursor, outbound feeds, webhooks | 2, 5 | M |
| 7 | #9 | Source health, fixtures and nightly contract tests | 1 | M |
| 8 | #10 | Source adapter framework and registry split | 7, 3 | M |
| 9 | #11 | Coverage expansion (one child issue per regulator) | 8 | S each |
| 10 | #12 | Published dataset release | 2, 6 | S |
| 11 | #13 | Digests and notifications | 6, 4 | S |
| 12 | #14 | Batch LLM classifier runner | 4 | S |

Sizes: S is a day or two, M is roughly a week, for one engineer or one agent session with review.

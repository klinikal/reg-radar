# reg-radar

Open-source regulatory intelligence tool for European (EU and UK) entities. It collects
news, warnings and publications from EEA financial regulators (plus the UK's FCA) on a
daily schedule, stores them in SQLite, and exposes the database via an MCP server so any
AI agent can query it. **Bring your own agent** — reg-radar ships clean structured data,
a web UI and deterministic classification; no model is called for you.

## Prerequisites

- **[Docker](https://www.docker.com/products/docker-desktop/)** (or Docker Engine + Compose on Linux).
- **Git**, to clone the repo.

## Quick start

```bash
git clone https://github.com/klinikal/reg-radar
cd reg-radar
cp .env.example .env
docker compose up -d
docker compose exec collector reg-radar setup       # choose what to collect
docker compose exec collector python -m collector.main --once   # first run
```

`docker compose up -d` starts `collector`, `mcp` (query server, `http://localhost:8000/mcp`),
`webui` (`http://localhost:8080`) and `flaresolverr`. `reg-radar setup` walks you through
jurisdictions and regulators, tells you roughly how much a selection will collect before
writing anything, and writes `config/modules.yaml`.

Connect an agent (Claude Code shown; any MCP-compatible client works):

```bash
claude mcp add --transport http reg-radar http://localhost:8000/mcp
```

Then give it [`skills/reg-radar-onboarding/SKILL.md`](skills/reg-radar-onboarding/SKILL.md)
to build your private operator context, and
[`skills/reg-radar-operator/SKILL.md`](skills/reg-radar-operator/SKILL.md) for how to
navigate the data day to day.

## What you get

- **Canonical documents** — every item as Markdown + frontmatter, plus the original
  artefact. [wiki](wiki/content/concept-canonical-document-format.md)
- **Taxonomy and classification** — objective labels with full provenance (rule, LLM,
  human, source default). [wiki](wiki/content/concept-taxonomy-classification.md)
- **Profiles** — a saved filter (jurisdictions, activities, instruments) applied
  identically everywhere. [wiki](wiki/content/concept-profiles-activities.md)
- **Change stream** — "everything since cursor X", behind export, feeds, webhooks and
  digests. [wiki](wiki/content/concept-export-sync-contract.md)
- **Source adapters** — adding a regulator is one config file plus one test.
  [wiki](wiki/content/concept-source-adapter-framework.md)
- **Batch LLM classifier** — `reg-radar classify` works the backlog unattended.
  [wiki](wiki/content/cli-classify.md)
- **MCP server** — 17 query/write tools over the collected data.
  [wiki](wiki/content/component-mcpserver.md)
- **Web UI** — browse, search and reclassify at `http://localhost:8080`.
  [wiki](wiki/content/component-webui.md)

## Reference

Everything else — every module, config file, script, workflow and runbook — lives in
[`wiki/README.md`](wiki/README.md), a per-component reference kept current alongside the
code (see [`wiki/SKILL.md`](wiki/SKILL.md) for how it's maintained).

- [CONTRIBUTING.md](CONTRIBUTING.md) — adding or fixing a regulator source
- [CHANGELOG.md](CHANGELOG.md)
- [wiki/README.md](wiki/README.md) — the full technical reference

## Naming

The product is **reg-radar** (hyphenated). The Python package stays `regradar` (hyphens
aren't valid in a Python import name) — only the console script, Docker/compose names,
the MCP server name and other user-facing strings use the hyphenated form.

## Licence

MIT. See [LICENSE](LICENSE) and [NOTICE](NOTICE) (reg-radar bypasses Cloudflare-protected
sources using [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr), MIT licence).

## Copyright

(c) Klinikal Limited, 2026.

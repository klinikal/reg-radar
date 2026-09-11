---
title: Inactive and Degraded Sources
type: config
sources: [config/regulators, config/regulators/_template.yaml]
last-verified: 2026-09-11
---

## Purpose

Every regulator or source that is present in `config/regulators/` but is not currently
collecting, and every source that collects but produces something less than it should.
It exists so that "why is there nothing from Portugal" has an answer in one place
instead of in a YAML comment nobody opens.

## How it works

An entry is inactive in one of three ways, and the distinction matters when deciding
whether to spend time on it:

1. **Defined with no sources.** The regulator entry exists, `enabled: false`, and carries
   no source blocks. Someone looked, found no machine-readable route, and recorded what
   they found. These are not broken; they were never built.
2. **Built and then paused.** A source exists and once worked, or looked like it would,
   and is now `enabled: false` with the reason in a comment above it.
3. **Collecting but degraded.** Still enabled, still producing items, but losing
   something on the way. These are the dangerous ones, because monitoring reports them
   as healthy.

The registry is the source of truth in all three cases. This page is a summary of it, and
if the two disagree, the YAML is right and this page is stale.

## Inputs

`config/regulators/*.yaml`, specifically the `enabled:` flag at regulator and source
level and the comment block above each disabled entry.

## Outputs and side effects

None. This page is documentation. Nothing reads it.

## How to run or invoke

To regenerate the counts below:

```
python -c "
import yaml, glob
for f in sorted(glob.glob('config/regulators/*.yaml')):
    if f.endswith('_template.yaml'): continue
    for r in yaml.safe_load(open(f)) or []:
        if not r.get('enabled', True):
            print('regulator off:', r['id'], len(r.get('sources') or []), 'sources')
        for s in (r.get('sources') or []):
            if not s.get('enabled', True):
                print('source off:   ', r['id'] + '/' + s['label'])
"
```

## Defined, but no working route exists

Eleven regulators are entered with no sources at all. Each was investigated and the
finding recorded; none is a bug.

| Regulator | Why nothing is collected |
|---|---|
| `be-nbb` | No feed. Email subscription only, via Flexmail. |
| `fr-acpr` | No RSS, and the site is WAF-blocked. Email subscription only. |
| `it-consob` | Radware Bot Manager blocks all automated access. |
| `it-bancaditalia` | No feed found. |
| `it-oam` | The OAM VASP register stopped operating on 1 July 2026. MiCA CASPs are now supervised by CONSOB and Banca d'Italia directly, so there is nothing left to collect. |
| `lv-latvijasb` | Blocks automated requests, no feed. `fktk.lv` now permanently redirects here. |
| `pt-cmvm` | A full JavaScript SPA on OutSystems. No feed, and nothing server-rendered to scrape. |
| `pt-bdp` | Cloudflare-protected, no news feed. The only feed found is a podcast. |
| `ro-bnr` | No feed. Every `/rss/*` path returns a skeletal exchange-rate shell with no items. |
| `si-atvp` | Cloudflare-protected. Needs a manual browser check. |
| `si-bsi` | No feed. `/rss` redirects to `/en/rss`, which 404s. |

Several of these would become possible with the `browser` adapter and a real browser
binary. That is a deliberate cost decision, not an oversight.

## Built, then paused

| Source | Why it is off |
|---|---|
| `dk-dfsa/news` | A Blazor/SignalR application. Neither plain HTTP nor FlareSolverr renders it. Disabled rather than faked. |
| `es-cnmv/sector_information` | The feed is real and correctly titled and returns a valid envelope with zero items. It has produced nothing in this install's entire history, while CNMV's other two feeds are healthy. |
| `hu-mnb/latest_releases` | Newest item is from June 2020, one item collected ever. MNB's English feeds are largely abandoned; two stopped in 2015, one of them named `teszt cimke`. MNB stays covered by its three working feeds. |
| `ae-fsra/announcements` | Works, 1,113 items, but `adgm.com`'s sitemap `lastmod` is a last-edit timestamp rather than a publication date. Of twelve checked against the date each page states, three matched, and 660 share one migration timestamp. Confidently wrong dates are worse than none. |
| `us-cfpb/enforcement_actions` | Healthy feed, newest item 2025-09-22. The documented CFPB enforcement dormancy, not a fault. Re-enable when enforcement resumes. |
| `us-sec/testimony` | Works, but congressional testimony is episodic and the newest item was 210 days old at capture. Off until US testimony is specifically wanted. |

Each keeps its captured fixture, so re-enabling is a one-line change rather than a
re-investigation.

## Collecting, but degraded

These report healthy. Monitoring cannot see either problem, which is the point of writing
them down.

| Source | What is lost |
|---|---|
| `lt-lb/publications_en` | Every publication date. `lb.lt` answers a direct fetch with a Cloudflare challenge, so it must use FlareSolverr, which returns what the browser rendered. Chromium renders XML through its own viewer and mangles the dates before we ever see them. Items keep title and link and get `date_inferred: true`. Not recoverable without raw response bytes, which FlareSolverr does not expose. |
| `uk-fca/news`, `uk-fca/warnings` | One hour, during British Summer Time. The feed publishes Europe/London and `parse_text_date()` reads a naive datetime as UTC. Harmless for ordering, and it cannot cause duplicate ids because the collector and the repair script derive dates identically. |

## Deliberately never built

Not failures. Recorded so nobody re-investigates them.

- **SEC litigation releases.** No feed exists and the Federal Register does not carry them.
  The listing is HTML only, and `www.sec.gov`'s HTML path returns a rate-limit 403 from
  every network tried. See [Regulators Registry](config-regulators.md).
- **FDIC press releases.** The only feed is third-party GovDelivery, and 900KB of its
  909KB response is email-template markup that the RSS adapter would store as each item's
  body.
- **FinCEN, NASAA, MSRB, NFA.** WAF-protected, geo-blocked outside the US, or dropped on
  relevance during planning.
- **55 MFSA circular categories.** Present and deliberately off. This is a curation
  choice about what an operator wants to read, not a source problem, which is why they
  are not listed individually here.
- **Sanctions lists.** Deferred to their own design question, since diffing per
  designation would put named individuals into a public git history.

## How to test

Nothing here is executable. To confirm this page still matches the registry, run the
snippet above and compare, or run the health check, which only ever probes enabled
sources:

```
python -m scripts.source_health
```

## Boundaries

Must never be the place a reason is recorded first. The registry entry is where an
operator and a coding agent will both look, so the comment above the entry is the record
and this page is the index. Must never list a source as working; enabled and working are
different claims, and the two degraded sources above are the reason that distinction
matters.

## Related pages

[Regulators Registry](config-regulators.md), [Collector Adapters](component-collector-adapters.md), [Source Health](script-source-health.md)

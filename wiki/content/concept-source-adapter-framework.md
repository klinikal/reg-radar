---
title: Source Adapter Framework
type: concept
sources: [collector/adapters/base.py, collector/adapters/registry.py]
last-verified: 2026-09-10
---

## Purpose

A source framework where adding a regulator is one config file plus one
test: each `sources[].type` maps to a registered adapter class with one
`fetch()` method, and everything after fetch is shared and adapter-agnostic.

## How it works

Each adapter implements `fetch(source_cfg, ctx) -> list[RawDocument]`,
where `RawDocument` carries url, title, published_at, body bytes, mime and
the source's `labels`. v1 types `rss`, `scrape` and `page_watch` become
adapters under this contract. `sitemap`, `json_api` and `browser`
(Playwright) are the first additions. Everything after fetch, extraction,
hashing, classification, storage, artefact write, is shared code, common
to every adapter type.

Every source gets an HTML or feed fixture under
`tests/fixtures/sources/<regulator>/<label>.html` and a contract test
asserting the adapter yields at least one item with a title, url and date
from that fixture. A nightly workflow runs the same test against the live
URL and opens or updates a GitHub issue per source on failure.

The registry splits into `config/regulators/<jurisdiction>.yaml`, loaded
as a directory (one file per jurisdiction, rather than one flat file).

## Inputs

A source config entry (`source_cfg`) from the regulator registry, a fetch
context (`ctx`), and for the nightly contract test, either the fixture file
or the live source URL.

## Outputs and side effects

A list of `RawDocument` objects passed into the shared extraction,
classification and storage pipeline. The nightly workflow additionally
opens or updates a GitHub issue per failing source.

## How to run or invoke

Not applicable at this level. See
[Collector adapters](component-collector-adapters.md) for the adapter
implementations and how the registry is loaded.

## How to test

Not applicable at this level. See
[Collector adapters](component-collector-adapters.md) for the contract
test command and how fixtures are organised.

## Boundaries

An adapter's `fetch()` must only return `RawDocument` objects, it must
never itself perform extraction, hashing, classification or storage, those
stay shared and adapter-agnostic. Every source must have a fixture and a
contract test before it ships.

## Related pages

[Principles](concept-principles.md)
[Canonical Document Format](concept-canonical-document-format.md)
[Collector adapters](component-collector-adapters.md)

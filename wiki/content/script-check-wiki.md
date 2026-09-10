---
title: Check Wiki
type: script
sources: [scripts/check-wiki.py]
last-verified: 2026-09-10
---

## Purpose

Checks `wiki/` for coverage and internal consistency: every source file has a page, every
page's claims are structurally sound, every link resolves. Stdlib only, deterministic.

## How it works

Walks `regradar/`, `collector/`, `mcpserver/`, `webui/` and `scripts/` (skipping
`__pycache__/`, `*.pyc`, `requirements.txt` and `__init__.py`) and builds the set of real
source files. Reads every page under `wiki/content/` (except `_template.md`), parses its
frontmatter, and checks: every `sources:` path exists; every listed directory's contents
count as covered; `last-verified` is non-empty; `type` is one of the allowed values; no
two pages share a `title`; every required heading is present; no em dash appears; every
markdown link resolves. Finally checks every content page is linked at least once from
`wiki/README.md`.

## Inputs

The repo tree under the five scanned directories, and every file in `wiki/content/*.md`
plus `wiki/README.md` and `wiki/SKILL.md`.

## Outputs and side effects

Prints one `FAIL:` line per problem to stdout and exits 1, or prints one `OK:` summary
line and exits 0. No files are written.

## How to run or invoke

```bash
python3 scripts/check-wiki.py
```

## How to test

Running it against a clean wiki is its own test: `python3 scripts/check-wiki.py` should
print `OK: ...` and exit 0.

## Boundaries

Only checks what's mechanically checkable (coverage, links, structure). It cannot verify
that a page's prose is actually true or that a page has kept up with its source; that
needs a manual read-through, see `wiki/SKILL.md`'s "Checking accuracy" section.

## Related pages

[wiki/README.md](../README.md), [wiki/SKILL.md](../SKILL.md)

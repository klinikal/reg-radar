---
title: Relock Script
type: script
sources: [scripts/relock.sh, collector/requirements.lock, mcpserver/requirements.lock, webui/requirements.lock]
last-verified: 2026-10-26
---

## Purpose

Regenerates the hashed lockfiles that each Docker image and CI job installs from, so every
dependency (including transitive ones) is pinned to an exact version and hash.

## How it works

`scripts/relock.sh` runs `uv pip compile --universal --python-version 3.11 --generate-hashes`
once per component. The direct pins in each `requirements.txt` are the input; the matching
`requirements.lock` is the output. `playwright` is collector-image-only, so the mcpserver and
webui locks are compiled from their own `requirements.txt` plus the collector's with the
`playwright` line removed.

## Inputs

`collector/requirements.txt`, `mcpserver/requirements.txt`, `webui/requirements.txt`. Needs
`uv` on the PATH and network access to the package index.

## Outputs and side effects

Overwrites `collector/requirements.lock`, `mcpserver/requirements.lock` and
`webui/requirements.lock`. Uses a temp directory that is removed on exit. Touches nothing else.

## How to run or invoke

Run after changing any `requirements.txt` or to pick up a security fix, then commit the lockfiles:

    scripts/relock.sh

## How to test

Re-run and confirm `git diff --stat` shows no change on a clean tree. To check a lockfile for
known vulnerabilities (`osv-scanner scan source -r .` does not see `*.lock` files):

    osv-scanner -L collector/requirements.lock

## Boundaries

Never edit a `requirements.lock` by hand. Never add a dependency to a lockfile without adding
the direct pin to the matching `requirements.txt`.

## Related pages

[Maintenance Scripts](script-maintenance.md)

---
name: reg-radar-wiki
description: >
  Read, write, and maintain reg-radar's wiki at wiki/. Use when the user asks to
  look something up in the wiki, add or update a wiki page, audit the wiki
  for accuracy or drift, or run its coverage checker. Portable across coding
  agents: nothing in this skill assumes a particular model or harness.
---

# reg-radar wiki

This skill governs `wiki/` in the reg-radar repo: a per-component reference,
one page per file or concept, in a fixed template, kept current alongside
the code. It exists so both an operator and a coding agent can trust it
without re-deriving facts from source every time.

## Reading the wiki

Start at [`README.md`](README.md). It links every page once, grouped by kind
(Concepts, Components, CLI, Config, Scripts, Jobs, Runbooks). Follow a link
rather than guessing a filename. A page names its `sources:` in the
frontmatter; when you need more detail than the page gives, read the source
file directly rather than assuming the page is exhaustive, it deliberately
isn't (see Conventions below).

Every page's `last-verified` date is a claim about when someone last
checked it against the source, not about when the underlying code last
changed. Treat a page as reference, not as proof current behaviour matches
what it says; if the source file looks newer than `last-verified`, verify
before relying on the page.

## Conventions (every page, no exceptions)

- Fixed shape: copy [`_template.md`](content/_template.md) exactly, frontmatter and
  headings. Never add or remove a heading, never reorder them.
- Frontmatter: `title` (a real Title Case name, never a filename or
  placeholder), `type` (one of `concept`, `component`, `cli`, `config`,
  `script`, `job`, `runbook`), `sources` (repo-relative paths this page is
  derived from, as a bracketed list), `last-verified` (an ISO date).
- No em dashes, anywhere, ever. Use a comma, colon, or period instead.
- State only what the cited source actually says. If you're not sure a
  claim is correct from the source you have, write `[UNVERIFIED]` before it
  rather than asserting it as fact. Never invent a flag, a default, a
  behaviour, or a rule that isn't in the source.
- Keep pages short. Prefer a link to the source file over restating its
  contents at length, the wiki is a map, not a copy of the territory.
- Links: use standard relative markdown links, a bracketed label followed
  by a parenthesized filename, for another wiki page in the same
  `content/` directory, or a relative path like `../../scripts/gather.py`
  for a source file outside the wiki.
- A directory can appear in `sources:` (e.g. `collector/adapters/`) to mean
  "this page covers everything under here"; use that only for genuinely
  cross-cutting pages (a component grouping many similarly-shaped files, a
  registry of many similar config files), not as a shortcut to avoid
  writing a page for one file.

## Generating a new page

Triggered when a new file lands under `regradar/`, `collector/`,
`mcpserver/`, `webui/` or `scripts/`, or a new concept needs documenting (a
new contract, a new cross-cutting flow).

1. Read the actual source file(s) in full before writing anything. Do not
   write a page from a filename, a commit message, or a guess about what a
   file "probably" does.
2. Copy [`_template.md`](content/_template.md), fill in the frontmatter, and write
   each section from what you just read. Follow the Conventions above.
3. Add the new page to [`README.md`](README.md)'s index, in the right
   section, one line, linked once.
4. Run `python3 scripts/check-wiki.py`. Fix anything it reports before
   considering the page done.

## Updating an existing page

Triggered whenever a change touches a file a wiki page cites in its
`sources:`, or when an audit finds the page has drifted from the code.

1. Re-read the current source file, not the old page; the page is what
   might be stale.
2. Edit only what changed. Don't rewrite unrelated sections or restyle
   prose that's still accurate.
3. Bump `last-verified` to the date of this check, even if the page's text
   didn't need to change, that date is the claim "someone confirmed this
   against the source on this day."
4. Run `python3 scripts/check-wiki.py`.

Any change to `regradar/`, `collector/`, `mcpserver/`, `webui/` or `scripts/`
updates the matching wiki page in the same commit, not as separate
follow-up work.

## Checking accuracy (auditing the wiki without changing code)

1. For each page (or a sampled subset, for a large sweep), read its
   `sources:` files fresh and compare every claim in the page against them.
2. Flag, don't silently fix, anything you're not confident about: a claim
   that isn't directly supported, a stale detail, a command that no longer
   matches the script's actual flags.
3. For claims you do fix, re-verify against the source before writing the
   correction, don't replace one guess with another.
4. Run `python3 scripts/check-wiki.py` at the end regardless of whether you
   changed anything, it catches structural drift (a moved file, a broken
   link) that a content read-through won't.

## What the checker actually verifies

`python3 scripts/check-wiki.py` is deterministic and stdlib-only. It checks,
and only checks, what's mechanically checkable:

- every file under `regradar/`, `collector/`, `mcpserver/`, `webui/` or
  `scripts/` is named in some page's `sources:`;
- every `sources:` path resolves to a real file or directory;
- every link in `wiki/*.md` resolves, including in `README.md` itself;
- every content page has all eight required headings and a non-empty
  `last-verified`;
- every content page's `type:` is one of the allowed values;
- no two pages share a `title:`;
- every content page is linked at least once from `README.md`;
- no page contains an em dash.

It does **not** and cannot check whether a page's prose is actually true,
whether a described command still behaves the way the page says, or
whether a page is stale relative to a source that changed without the page
being touched. Those need a read-through against the actual source, which
is what the "Checking accuracy" section above is for; the checker only
proves the wiki is internally coherent, not that it's correct.

## Delegating drafting to a cheaper model

For high-volume, repetitive-shape drafting (writing many pages to the same
fixed template at once), it's reasonable to delegate to a faster, cheaper
model than the one doing the reading and reviewing. This works well
because the judgment-heavy part, deciding what's true and what the page
should say, already happened before delegation; what's left is mechanical
reformatting into a fixed shape.

**Do:**

- Read every source file yourself first, or verify it was read in full by
  whatever drafted the page. Never trust a page written from a filename, a
  commit message, or a guess about what a file "probably" does.
- Give the delegate the literal template (headings, frontmatter shape) and
  the exact target frontmatter values (`title`, `type`, `sources`,
  `last-verified`) for each page. Don't leave it to infer a title from a
  filename, that produces titles like the filename itself instead of a
  real name.
- Batch thematically related pages into one delegate call (e.g. "these 4
  config files") rather than one call per page; it's faster and the
  delegate keeps a consistent voice across the batch.
- State every constraint explicitly, every time: no em dashes, mark
  unverified claims, use plain markdown links only. A delegate will not
  infer these from house style; it will only follow what's in its prompt.
- Review every page the delegate writes before it counts as done. Read it
  against the source text it was given. Delegating drafting is not
  delegating verification, that stays with the reviewer.

**Don't:**

- Don't let a delegate decide what belongs in a page's `sources:`, or any
  other judgment call about scope or boundaries. Decide that separately and
  hand it down as an instruction.
- Don't trust a delegate's cross-page links or claims about another page's
  content without checking. It doesn't have the rest of the wiki in view
  unless given that context explicitly.
- Don't skip the review pass because the batch "looked fine" in the
  delegate's own summary of what it did. A summary describes what was
  intended, not necessarily what was written; read the actual files.
- Don't delegate the checker script or the index file, or any file where a
  subtle logic bug would silently break coverage. Those are worth writing
  directly.

## Related pages

[wiki/README.md](README.md)

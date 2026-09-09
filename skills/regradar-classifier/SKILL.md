---
name: regradar-classifier
description: Use when asked to classify unclassified regradar items on the document_type or instrument axis, work through the regradar classification backlog, or "help regradar figure out what these items are". Triggers on "classify these regradar items", "work the unclassified backlog", "run the regradar classifier skill", or a direct request to label document_type/instrument for regradar via the MCP server.
---

# regradar Classifier

Classifies regradar items on the `document_type` and `instrument` taxonomy axes (architecture doc §4.1) via the MCP write interface (§4.5), so an agent can work the classification backlog without the repo holding any provider keys or picking a vendor (§4.6). No code here calls an LLM API directly - the agent reading this skill *is* the classifier.

**Read your operator context first, if it exists:** check `skills/regradar-operator/refs/context.md` (relative to repo root). If present, it names the entity this instance serves - that tells you which items are *worth reading closely* (prioritise), never which label to assign. The taxonomy is objective: what an item *is* (its document type, the legal instruments it substantively concerns) does not change based on who's asking. Do not label something `mica` just because the operator is a Maltese CASP and MiCA is relevant to them - label it `mica` only if the document is actually about MiCA.

## Before you start

Call `get_taxonomy_tool()`. It returns every axis, its cardinality (`one` or `many`), and every valid value with its id, name, and aliases. Use this - not memory, not this file - as the source of truth for valid values; the taxonomy grows by PR and this skill file is not guaranteed to list every value in it.

- `document_type` is single-valued: pick exactly one, or leave unset.
- `instrument` is multi-valued: an item can name multiple instruments (e.g. a notice about MiFID II and MAR together), or none.

## Working the backlog

1. Pick one axis to work (`document_type` or `instrument` - don't mix in one pass, it's easier to stay consistent).
2. Call `get_unclassified_tool(axis=<axis>, limit=50)` to get a page of items with no live classification on that axis. Each item includes `title`, `body_md` (truncated to ~6000 characters), `regulator`, `jurisdiction`, and `labels` (its current classifications on other axes, for context - e.g. knowing `document_type` is already `warning` helps judge `instrument`).
3. Read `title` and `body_md`. For a short item, the title alone is often enough; for anything ambiguous, read the body.
4. Decide a value (or, for `instrument`, a set of values) and a confidence 0.0-1.0. **Prefer leaving an axis unset over guessing below 0.5** - unset is a real, visible state (architecture doc principle 5); a low-confidence guess just adds noise to `scripts/eval_classifier.py`'s precision numbers without helping anyone.
5. Batch up to 100 decisions and write them with `classify_items_tool(classifications=[...])`. Use `by: "llm:<provider>/<model>"` (e.g. `"llm:anthropic/claude-sonnet-5"`) - name what's actually classifying, not a placeholder. Check the `errors` list in the response: an entry with a typo'd value still gets reported even though the rest of the batch was written.
6. Repeat with `offset` (or just call `get_unclassified_tool` again - it always returns what's still unclassified) until the backlog for that axis is empty or you've done enough for this session.

`classify_item_tool(item_id, axis, value, by, confidence, classifier_id?)` is the single-item equivalent - use it when correcting one item rather than batching, e.g. after re-reading something the batch pass got wrong.

## document_type: use these strictly as defined

Don't extrapolate meaning beyond the taxonomy name/description returned by `get_taxonomy_tool()`. Common judgment calls:

- A notice that a specific firm/website is unauthorised, a clone, or a scam -> `warning`, even if the regulator's own feed calls it "news."
- A fine, licence cancellation/surrender, or formal sanction -> `enforcement`.
- "Consultation Paper", "CP26/12", or "consults on" -> `consultation`. A **feedback statement** or **final report** responding to a prior consultation -> `final_guidance`, not `consultation` - the consultation already closed.
- A formal RTS/ITS/delegated act -> `technical_standard`. General "technical advice" or "guidelines" without that formal instrument status -> `final_guidance`.
- A change to a public register or list of authorised/unauthorised firms -> `register_update`.
- Ordinary announcements, annual reports, MoUs, conference notices -> `press_release`.
- A monetary policy statement, a speech, or a standalone statement with no other clear category -> `speech`.

## instrument: only when substantively about it

Label an instrument only when the document is genuinely about that legal instrument or regime - not because it's mentioned in a boilerplate footer, a "see also" list, or a single passing reference. A DORA-labelled item should be readable as "this is about DORA," not "DORA is mentioned somewhere in this." When in doubt, leave it off; `instrument` is multi-valued so under-labelling costs less than over-labelling (a false instrument label pollutes every profile filtered on it).

## Five worked examples

**1. Clear warning, no instrument**
Title: *"FMA warns against offerings by GBM Global Inc. ('BitMart')"*
`document_type = warning`, confidence 0.95. No `instrument` - crypto-adjacent by the firm's name, but the notice itself is a plain unauthorised-firm warning, not substantively about a named legal instrument.

**2. Enforcement naming a specific regulation**
Title: *"Violation of the Market Abuse Regulation: Bafin imposes administrative fines for market manipulation"*
`document_type = enforcement`, confidence 0.95. `instrument = mar`, confidence 0.9 - the regulation is named as the basis for the fine, not a passing mention.

**3. Consultation vs. feedback statement**
Title: *"Feedback Statement on the Consultation on the Proposed Amendments to the Insurance Rules"*
`document_type = final_guidance`, confidence 0.8 - it's a feedback statement (the consultation already ran), not a live consultation. Do not label `consultation` just because the word appears in the title.

**4. Formal technical standard naming an instrument**
Title: *"Regulation (EU) 2022/2554 on Digital Operational Resilience for the Financial Sector - Register of Information Additional Data Quality Checks"*
`document_type = register_update`, confidence 0.7 (it's about the DORA Register of Information return, not a new legislative text). `instrument = dora`, confidence 0.9 - the regulation is the whole subject.

**5. Ambiguous title - read the body, or leave unset**
Title: *"Company Service Provider - Ref: 2026-34"*
Title alone gives no reliable signal. Read `body_md`: if it's a licence grant/removal notice, `document_type = register_update`; if it's a warning, `warning`; if the body is equally unhelpful (a bare reference number with no descriptive text), leave `document_type` unset rather than guess - call `classify_items_tool` for the items you *can* call confidently and skip this one.

## Checking your own accuracy

`scripts/eval_classifier.py` compares live `by: rule` and `by: llm:*` rows against `tests/fixtures/classification/gold.csv`, a hand-labelled sample, and prints precision/recall per axis and per value. If you're running a large classification pass, ask the operator to run it afterward (or run it yourself if you have shell access) - self-reported confidence from an LLM is not calibrated, and this is the only way anyone finds out where it's wrong.

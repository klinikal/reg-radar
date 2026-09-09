# Coverage expansion: legislation sources, new jurisdictions, and re-enabling disabled sources

**Size:** S per child issue, parent is tracking only  **Depends on:** #10 source adapter framework and registry split  **Design doc:** docs/design/regradar-architecture.md §7, §8 (build order, row 9)

## Why

RegRadar's registry today (`config/regulators.yaml`) is EEA-plus-UK regulator commentary: news, warnings, consultations and guidance pages. It has no coverage of the legislative texts those regulators write guidance about, no coverage outside Europe, and 67 already-defined sources sitting disabled because a selector was never verified against a real blocker. Once #10 lands (one adapter class per source type, a fixture-and-contract-test requirement, and a registry that can be split into `config/regulators/<jurisdiction>.yaml`), adding a regulator becomes "one config file plus one test" per design doc §2. This issue is the tracking parent for that expansion: it fixes the order new jurisdictions get added in, tells whoever opens a child issue what "done" looks like, and turns the current disabled-source list into a worked backlog instead of a pile of `enabled: false` entries nobody revisits.

## Scope

- A GitHub milestone or project board (whichever this repo already uses) listing one child issue per regulator/source group below, opened in priority order once #10 merges.
- This document as the reference every child issue links back to instead of restating the "why".
- No source code changes in this issue itself: it is planning and prioritisation only.

## Non-goals

- Building any adapter. That is #10.
- Translation of non-English content. See the language note below.
- A relevance-scoring or "which regulator matters to me" layer: that is what profiles (design doc §5) are for; this issue only gets documents into the corpus.
- Historic backfill design for new sources: reuse the existing `scripts/backfill_*.py` pattern per source once the regular collector run is proven; a backfill script is optional per child issue, not required for its definition of done.

## Priority order and justification

1. **EUR-Lex and the Official Journal of the EU** (`jurisdiction: eu`, `document_type: legislation` and `technical_standard`). The registry collects regulator commentary about MiCA, DORA and the rest but not the legislative texts and delegated/implementing acts themselves, the highest-authority source for every `instrument` value already in `taxonomy.yaml`.
2. **FCA Handbook change log and FCA Policy Statements.** `uk-fca`'s existing `policy_and_guidance` scrape source already includes `handbook` and `policy%20statements` in its category filter string, so audit live first: this may be partially covered already and the child issue may be "add a fixture that proves it" rather than "add a source." If the FCA search results page doesn't reliably surface Handbook instrument-by-instrument changes (a different artefact from a Policy Statement PDF), a dedicated source against the Handbook site itself is needed.
3. **US federal: SEC, CFTC, FinCEN, OCC, Federal Reserve, FINRA** (`jurisdiction: us`). The US is entirely absent today (README: EEA + UK only), despite being one of the largest active regulatory regimes for crypto/CASP-adjacent activity. Most of these agencies publish plain RSS for press releases and enforcement actions, so this group is likely the cheapest set of wins in this list; verify per agency rather than assume.
4. **Switzerland FINMA** (`jurisdiction: ch`). A common comparator regime (the DLT Act and FINMA guidance) outside both EEA and UK/US scope; zero coverage today.
5. **Singapore MAS** (`jurisdiction: sg`). A major APAC digital-payment-token licensing regime under the Payment Services Act, frequently referenced alongside MiCA; zero coverage today.
6. **Hong Kong SFC and HKMA** (`jurisdiction: hk`). HK's VASP licensing regime (SFC) plus banking-side guidance (HKMA); zero coverage today.
7. **UAE: VARA, DFSA, FSRA at ADGM** (`jurisdiction: ae`, with DIFC and ADGM likely needing their own ids with `parents: [ae]` per the jurisdictions DAG in design doc §4.2). Three parallel crypto regimes in one country and an increasingly common CASP relocation destination. Expect this group to need scrape or browser adapters rather than RSS: audit first, budget accordingly.
8. **Liechtenstein FMA** (`jurisdiction: li`, `parents: [eu]`, an EEA-EFTA state, same pattern as Norway in the design doc's `jurisdictions.yaml` example). Named as a known gap in the README's Contributing section. Note: the README currently spells this "PFSA"; Liechtenstein's regulator is the FMA (Finanzmarktaufsicht Liechtenstein), flag and fix that README wording when this child issue opens.

Every new jurisdiction above will have few or no matching values in `taxonomy.yaml`'s `instrument` axis (which currently only covers EU and UK instruments). That is not a blocker (per design doc principle 5, unclassified items are visible by default), but each child issue should expect to pair its source config with a small `taxonomy.yaml` PR adding the relevant jurisdiction-appropriate instrument values (e.g. a US BSA/travel-rule instrument, a Swiss DLT Act instrument), or note in its own scope that it is deliberately deferring that and shipping `document_type`-only source defaults.

## Re-enabling the 67 currently disabled sources

Counted directly from `enabled: false` in `config/regulators.yaml` (both regulator-level and source-level), grouped by why:

**Cloudflare (2).** `pt-bdp` (Banco de Portugal), `si-atvp` (Slovenia ATVP): both flagged `cloudflare_protected: true`, both currently `sources: []`. These need no new adapter: the existing `scrape` + FlareSolverr path already works for `lt-lb` and `mt-mfsa`. Recommendation: do the manual browser check each note asks for, write selectors, re-enable. Cheapest group to clear.

**JS-rendered (2).** `pt-cmvm` (full OutSystems SPA, regulator-level disabled) and `dk-dfsa`'s one `news` source (Blazor/SignalR, confirmed FlareSolverr's plain request doesn't wait for the circuit to populate). Recommendation: the `browser` (Playwright) adapter design doc §7 adds as a first-class type is the direct unblock, since it can wait for a selector or network-idle state before scraping, which neither plain HTTP nor FlareSolverr's request-replay model does.

**No feed found (4).** `be-nbb`, `it-bancaditalia`, `ro-bnr`, `si-bsi`: all confirmed no RSS, email-subscription-only. Recommendation: try the new `sitemap` adapter first (a `sitemap.xml` with `lastmod` can act as a low-fidelity feed even with no RSS) and `json_api` if the listing page turns out to call an internal JSON endpoint. If neither applies, leave disabled: these may genuinely have no automatable surface, and that is fine per design doc principle 5, a stalled or absent source is visible, not silently missing.

**Other (59).** Two different situations, don't conflate them:
- *Genuinely blocked (4 regulators):* `it-consob` (Radware Bot Manager), `fr-acpr` (WAF-blocked), `lv-latvijasb` ("blocks automated requests") are bot/WAF protection that isn't literally Cloudflare, so FlareSolverr's Cloudflare-specific bypass may not apply. Recommendation: try the `browser` adapter as a heavier fallback (it can carry a full session further than FlareSolverr's model); if that still fails, these need a conversation with the regulator about automated access, which is out of scope for #10/#11 alone. `it-oam` is not blocked at all: its VASP register is decommissioned as of 1 July 2026 per its own notes. Recommendation: remove this entry from the registry rather than budget adapter work on it.
- *Deliberately scoped out, not blocked (55 sources):* every disabled `mt-mfsa` circular category. These were pre-filtered in v1 because there was no profile system to filter downstream, the README says as much ("Only categories relevant to a bitcoin-only CASP are enabled by default"). Now that v2 has profiles (design doc §5) filtering the whole corpus per operator, pre-filtering at collection time is the wrong layer. Recommendation: flip all 55 to `enabled: true` as a config-only change (no adapter work, no child issue really needed beyond the config PR and re-running the contract test suite from #9), and let profiles keep an operator who doesn't care about Solvency II circulars from seeing them.

## Definition of done for a single regulator child issue

- [ ] Registry entry added (in `config/regulators.yaml` or its issue-#10 successor directory), with `labels` (not `tags`) set per design doc §4.3, `preferred_language` set, and `jurisdiction` set to a lower-case id.
- [ ] That jurisdiction id exists in `config/jurisdictions.yaml`, with correct `parents` if it is not a top-level jurisdiction.
- [ ] A fixture under `tests/fixtures/sources/<regulator_id>/<source_label>.<html|xml>` per #9's convention.
- [ ] The offline contract test from #9 passes against that fixture.
- [ ] At least one successful live run against the real URL, confirmed manually or via the nightly workflow from #9.
- [ ] README's coverage count and jurisdiction list updated to include the new regulator.

## Non-English sources

Several likely additions in this list (and several already in the registry) are not English-language. `preferred_language` on the source is the only obligation here: set it accurately. Translation of non-English content is explicitly out of scope for this issue and for RegRadar generally; a downstream agent or classifier skill can translate on read if a user needs it.

## Open questions

- Should the 55 MFSA "other" sources be flipped to `enabled: true` in this issue's own PR, or does that need its own child issue given it touches an existing regulator rather than adding one? Recommend a single small PR, not a full child issue, since no new adapter or fixture set is needed beyond re-running the existing contract tests.
- Do DIFC and ADGM need their own `jurisdiction` ids distinct from `ae`, given they are separate legal/regulatory zones within the UAE? Recommend yes, following the `us`/`us-ny` pattern in design doc §4.2, but confirm during the UAE child issue rather than deciding it here.

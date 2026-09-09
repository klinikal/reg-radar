# Classification gold sample

`gold.csv` is the hand-labelled sample `scripts/eval_classifier.py` measures
precision/recall against (docs/issues/04-rules-engine-write-interface-llm
-classifier-skill.md). Columns: `item_id` (a real Task 9 item id, per
`collector.schema.make_item_id`), `axis`, `expected_value` (per
`config/taxonomy.yaml`), plus `title` and `regulator_id` kept for
readability/audit - the eval script only reads the first three.

## How rows were chosen

1. Every enabled source's captured fixture under `tests/fixtures/sources/`
   was parsed with the real production parser
   (`scripts.source_health.parse_fixture`, offline, no live fetch), giving
   real item ids and titles exactly as the collector would produce them.
2. Titles were de-duplicated and a diverse sample drawn across regulators.
3. Each title was hand-labelled by reading it (regulator context included,
   since the same wording means different things from different sources)
   against the `document_type` and `instrument` axes in
   `config/taxonomy.yaml`:
   - `document_type`: exactly one value per item, based on what kind of
     notice the title clearly is (a warning about an unauthorised/cloned
     firm -> `warning`; a fine/sanction/licence surrender -> `enforcement`;
     "Consultation"/"CP" -> `consultation`; a feedback statement or final
     guidelines/Q&A -> `final_guidance`; an RTS/ITS/delegated act -> a
     `technical_standard`; a register/authorisation-list change ->
     `register_update`; otherwise a press release/announcement ->
     `press_release`, or a policy statement with no other signal ->
     `speech`. Titles too ambiguous to call confidently from the title
     alone were left out rather than guessed.
   - `instrument`: only labelled where a specific legal instrument is
     named explicitly in the title (MiCA, MiFID II/MiFIR, DORA, AML/CFT,
     UCITS, the Prospectus Regulation) - per the issue's instruction to the
     LLM classifier skill, "label instruments only where the document is
     substantively about them," most titles correctly get no instrument
     label at all.
4. This is a title-only labelling pass (no full body text was read) - it is
   accurate for what a title-based rule or an agent skimming a title would
   see, and is intended as a first, replaceable baseline, not a definitive
   corpus. Re-running `eval_classifier.py` after any rules.yaml or taxonomy
   change is how drift from this baseline gets caught.

137 rows total (122 `document_type`, 15 `instrument`) across 27 regulators
and both EU/UK/national sources.

# Batch LLM classifier runner (unattended, pluggable provider)

**Size:** S  **Depends on:** #6 rules engine, classification write interface and LLM classifier skill  **Design doc:** docs/design/regradar-architecture.md §4.6

## Why

The classifier skill from issue #6 needs an MCP-connected agent sat in a chat session to work through the backlog. That is fine interactively, but nobody wants to babysit a terminal to classify thousands of backfilled items, and a scheduled job cannot drive a skill. Design doc §4.6 calls for a batch runner doing the same job unattended, against a provider of the operator's choosing, so classification runs on a schedule alongside the collector instead of depending on a human opening an agent session.

## Scope

- A `regradar classify` subcommand on `regradar/cli.py` (created in #4), with the runner in `regradar/classify.py` and providers under `regradar/providers/`.
- Pulls the backlog via `get_unclassified` (the function backing `get_unclassified_tool` from issue #6's write interface, §4.5), oldest first.
- A provider abstraction with four built-in providers: `anthropic`, `openai`, `ollama`, and `command`, which shells out to any CLI that reads a prompt and writes text, so a subscription-backed tool such as `claude -p` works with no API key.
- Writes results through the shared `write_classification` interface (§4.5) with `by=llm:<provider>/<model>`, never a bespoke insert.
- Flags: `--dry-run` (classify and print, write nothing), `--axis <axis>` (one taxonomy axis), `--limit <n>` (cap items this run).
- Rate limiting between provider calls, configurable requests-per-minute with a sensible default per provider.
- Cost logging: token usage and estimated cost per call for API providers; `command` and `ollama` log as unmetered.
- A fixed JSON output schema the model must return, validated against `taxonomy.yaml` (correct axis, known value ids, cardinality, confidence in range) before anything is written.

## Non-goals

- The interactive skill itself (`skills/regradar-classifier/SKILL.md`): that is issue #6.
- Provider-specific fine-tuning, prompt caching, or a batching API (e.g. Anthropic's Message Batches): a follow-on optimisation.
- A retry/backoff policy beyond simple rate limiting; provider outages are out of scope.
- Building `rules.yaml` or touching rule-based classification.

## Design notes

Provider interface, one method, so adding a fifth provider is one file:

```python
class Provider(Protocol):
    def classify(self, prompt: str) -> str: ...  # raw model output; runner parses and validates it
```

`anthropic` and `openai` call the respective API, enforcing the taxonomy-derived JSON schema via structured output or tool use where available, falling back to strict prompt instructions plus parsing otherwise. `ollama` and `command` share the same parse-and-validate path; `command` runs a configured shell command (e.g. `claude -p`), feeding the prompt on stdin and reading the response from stdout, needing no provider-specific code.

Fixed response schema, one axis per call, matching the `classifications` columns from §4.4:

```json
{
  "axis": "instrument",
  "values": [
    {"value": "mica", "confidence": 0.91},
    {"value": "amld", "confidence": 0.62}
  ],
  "reasoning": "one short sentence, logged but not stored"
}
```

Validation before write: `axis` must exist in `taxonomy.yaml` (version pinned at run start); every `value` must exist in that axis's vocabulary; `cardinality: one` axes reject more than one value; `confidence` must fall in `0.0` to `1.0`. A response failing validation is logged and skipped, never written as a low-confidence guess. `classifier_id` is a runner/version string, e.g. `regradar-classify/<provider>`, distinct from `by`.

Reuse the same taxonomy-rendering function the issue #6 skill uses for its prompt (axis values, `aliases`), so the two paths cannot drift on what they tell the model is valid. CLI shape: `regradar classify --provider anthropic --axis instrument --limit 200 --dry-run`, or `--provider command --command "claude -p"`.

## Acceptance criteria

- [ ] `regradar classify` runs end to end against at least one API provider and the `command` provider in a test setup (mocked HTTP, a stub script for `command`).
- [ ] Every write goes through `write_classification` with `by=llm:<provider>/<model>` and a populated `classifier_id`; no direct table inserts.
- [ ] `--dry-run` classifies and prints results but writes nothing. `--axis` restricts to one axis, omitted means all axes. `--limit` caps items per run.
- [ ] A response with an unknown axis, unknown value id, wrong cardinality, or out-of-range confidence is rejected, logged, and written nowhere.
- [ ] Rate limiting between provider calls is applied and configurable.
- [ ] Cost and usage are logged per run for anthropic and openai; command/ollama log without fabricating a cost figure.
- [ ] README documents all four providers and the `command` provider's shell-out contract.

## Open questions

- Before the README recommends `command` with a consumer subscription CLI (e.g. `claude -p`) for unattended, scheduled use, check that provider's terms of service permit automated use under a subscription plan rather than an API key. This should not ship a recommendation that turns out to violate a provider's terms.
- Should failed validations be quarantined for later manual review, or is a log line sufficient for v2?
- Is one axis per model call the right granularity, or should the runner request all axes in one call to cut cost, at the price of a larger and harder-to-validate response?

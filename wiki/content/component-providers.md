---
title: Providers
type: component
sources: [regradar/providers/]
last-verified: 2026-09-10
---

## Purpose

Provider plugin registry for `regradar classify`: one module per LLM (or
shell-command) backend, each implementing a shared `classify(prompt) -> str`
protocol so the classification runner can swap backends without special
casing.

## How it works

Every provider module lazily imports its SDK (or, for `command`, has no
external dependency) so the rest of the CLI works without every provider's
extra installed, only picking that provider requires it.

Provider protocol (one method, so a fifth provider is one file):

```
class Provider(Protocol):
    name: str
    model: str
    def classify(self, prompt: str) -> str: ...  # raw model output
```

Providers also carry `last_usage`, set after each `classify()` call:
`{"input_tokens": int, "output_tokens": int}` for a metered API provider, or
`None` for an unmetered one (ollama, command); the runner never fabricates a
cost for `None`.

`regradar/providers/__init__.py` holds `PROVIDER_DEFAULTS`, a `name ->
{model, rpm}` dict of sensible defaults (model ids and RPM ceilings age as
vendors ship new models/plans, so treat these as needing periodic
verification):

- `anthropic`: model `claude-sonnet-4-5-20250929`, rpm 50.
- `openai`: model `gpt-5-mini`, rpm 60.
- `ollama`: model `llama3.1`, rpm 120.
- `command`: model `None`, rpm 20.

`get_provider(name, model=None, command=None, rpm=None, **kwargs)`
instantiates the named provider, resolving `model` and `rpm` against
`PROVIDER_DEFAULTS` when not given explicitly, and raises `ProviderError`
for an unknown provider name or missing requirement (e.g. `command`
provider without `--command`). Sets `provider.rpm` on the returned instance
before handing it back.

Each provider file, `regradar/providers/anthropic.py`,
`regradar/providers/openai.py`, `regradar/providers/ollama.py`,
`regradar/providers/command.py`:

- **anthropic** (`AnthropicProvider`): requires the `anthropic` package and
  `ANTHROPIC_API_KEY` (or an injected `client`). Uses tool-use to force a
  structured response: `configure(axis, values, cardinality)` is called by
  the runner before each `classify()` to set the taxonomy axis/vocabulary in
  play (the fixed `classify(prompt)` signature has no room for this, so it's
  a separate hook). `_build_tool` builds a `submit_classification` tool
  whose JSON schema enumerates the allowed values, capped to `maxItems: 1`
  when `cardinality == "one"`. `classify()` forces `tool_choice` to that
  tool, extracts the first `tool_use` content block, and returns its input
  as a JSON string with `axis` added. Records `last_usage` from the
  response's `usage.input_tokens`/`output_tokens`.

- **openai** (`OpenAIProvider`): requires the `openai` package and
  `OPENAI_API_KEY` (or an injected `client`). Uses the JSON-schema response
  format to force structure: `_build_schema` builds a strict schema with an
  enum-constrained `value` per item and a `reasoning` string. `classify()`
  calls `chat.completions.create` with `response_format={"type":
  "json_schema", ...}`, parses the JSON content, and adds `axis` before
  returning it as a JSON string. Records `last_usage` from
  `usage.prompt_tokens`/`completion_tokens`.

- **ollama** (`OllamaProvider`): calls a local Ollama server's HTTP API via
  `httpx` (already a project dependency, so no lazy import). Base URL
  defaults to `http://localhost:11434` or the `OLLAMA_BASE_URL` env var;
  timeout 120s. No structured-output enforcement, relies on prompt
  instructions plus the runner's shared parse-and-validate path. `classify()`
  POSTs to `/api/generate` with `stream: false` and returns the `response`
  field verbatim. `last_usage` is always `None`.

- **command** (`CommandProvider`): shells out to any CLI that reads a
  prompt on stdin and writes text on stdout (stdlib `subprocess` only, no
  external dependency). `model` defaults to the invoked binary's basename if
  not given. `classify()` runs the command via `shlex.split`, passing the
  prompt as stdin, with a configurable timeout (default 120s). Raises
  `ProviderError` if the command is not found or times out, and
  `RuntimeError` if it exits non-zero (stderr, truncated to 500 chars, in
  the message). `last_usage` is always `None`. The module docstring notes
  operators must check their own tool's terms of service before using it
  for unattended, scheduled classification; it makes no vendor
  recommendation.

## Inputs

- `get_provider`: `name` (one of `anthropic`, `openai`, `ollama`,
  `command`), optional `model`, `command` (required for the `command`
  provider), `rpm`, and provider-specific `**kwargs` (e.g. `api_key`,
  `client`, `base_url`, `timeout`).
- Each provider's `classify(prompt)`: a prompt string; `configure(axis,
  values, cardinality)` is called first to set classification context.
- Env vars: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OLLAMA_BASE_URL`.

## Outputs and side effects

- `get_provider` returns a configured provider instance.
- `classify()` returns raw model output as a string (already JSON for
  anthropic/openai; raw text for ollama/command, validated downstream by the
  runner).
- Network calls (anthropic/openai SDK calls, ollama HTTP POST) or a
  subprocess invocation (command). Sets `self.last_usage` as a side effect
  of `classify()`.

## How to run or invoke

Not invoked directly; selected via `regradar classify --provider
<anthropic|openai|ollama|command> [--model ...] [--command ...]`.
[UNVERIFIED] exact CLI flag wiring lives in the classify command module, not
in this source; see [reg-radar classify](cli-classify.md) for the command
surface.

## How to test

[UNVERIFIED] No test command is stated in these source files; check
`code/tests/` for a `test_providers.py`-style file, likely using an injected
`client`/mocked `httpx`/mocked `subprocess.run` per provider.

## Boundaries

- Never requires a provider's SDK to be installed unless that provider is
  actually selected (lazy import per provider).
- Never fabricates `last_usage` for an unmetered provider (ollama, command);
  it stays `None`.
- `command` provider never makes a vendor recommendation or performs ToS
  checking itself, that is left to the operator.

## Related pages

[reg-radar classify](cli-classify.md)

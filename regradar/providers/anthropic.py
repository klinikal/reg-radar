"""Anthropic provider for `regradar classify` (docs/issues/12-batch-llm
-classifier-runner.md). Lazily imports the `anthropic` package - only
constructing this provider requires it installed (`pip install
'regradar[llm]'`), so `regradar classify --provider command` (or any other
provider) works without it.

Uses tool-use to force a structured response matching the taxonomy's valid
values for the axis in play (issue design note: "for anthropic use tool-use
with a JSON schema built from the taxonomy").
"""
import json
import os

from regradar.providers import ProviderError

_DEFAULT_MAX_TOKENS = 1024


def _build_tool(axis: str, values: list[str], cardinality: str) -> dict:
    value_schema = {
        "type": "object",
        "properties": {
            "value": {"type": "string", "enum": values},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        },
        "required": ["value", "confidence"],
    }
    items_schema = {"type": "array", "items": value_schema}
    if cardinality == "one":
        items_schema["maxItems"] = 1
    return {
        "name": "submit_classification",
        "description": f"Submit the classification decision for the {axis!r} taxonomy axis.",
        "input_schema": {
            "type": "object",
            "properties": {
                "values": items_schema,
                "reasoning": {"type": "string", "description": "One short sentence - logged, not stored."},
            },
            "required": ["values", "reasoning"],
        },
    }


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str, api_key: str | None = None, client=None):
        self.model = model
        self.last_usage = None
        self._axis = None
        self._values: list[str] = []
        self._cardinality = "one"

        if client is not None:
            self._client = client
            return

        try:
            import anthropic
        except ImportError as e:
            raise ProviderError(
                "provider 'anthropic' requires the 'anthropic' package - "
                "install it with `pip install regradar[llm]` or `pip install anthropic`"
            ) from e

        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ProviderError("ANTHROPIC_API_KEY is not set")
        self._client = anthropic.Anthropic(api_key=key)

    def configure(self, axis: str, values: list[str], cardinality: str) -> None:
        """Called by the runner before each classify() call to tell the
        provider which axis/vocabulary the next prompt targets - the fixed
        `classify(prompt) -> str` protocol has no room for this, so it's a
        separate hook rather than an extra classify() parameter."""
        self._axis = axis
        self._values = values
        self._cardinality = cardinality

    def classify(self, prompt: str) -> str:
        tool = _build_tool(self._axis, self._values, self._cardinality)
        response = self._client.messages.create(
            model=self.model,
            max_tokens=_DEFAULT_MAX_TOKENS,
            tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]},
            messages=[{"role": "user", "content": prompt}],
        )
        usage = getattr(response, "usage", None)
        if usage is not None:
            self.last_usage = {
                "input_tokens": getattr(usage, "input_tokens", 0),
                "output_tokens": getattr(usage, "output_tokens", 0),
            }
        block = next(b for b in response.content if getattr(b, "type", None) == "tool_use")
        payload = dict(block.input)
        payload["axis"] = self._axis
        return json.dumps(payload)

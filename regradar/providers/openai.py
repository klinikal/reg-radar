"""OpenAI provider for `regradar classify` (see
../../wiki/content/cli-classify.md). Lazily imports the `openai` package - see
regradar/providers/anthropic.py's module docstring for why.

Uses the JSON-schema response format to force a structured response (issue
design note: "for openai use JSON-schema response format").
"""
import json
import os

from regradar.providers import ProviderError


def _build_schema(values: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "values": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "value": {"type": "string", "enum": values},
                        "confidence": {"type": "number"},
                    },
                    "required": ["value", "confidence"],
                    "additionalProperties": False,
                },
            },
            "reasoning": {"type": "string"},
        },
        "required": ["values", "reasoning"],
        "additionalProperties": False,
    }


class OpenAIProvider:
    name = "openai"

    def __init__(self, model: str, api_key: str | None = None, client=None):
        self.model = model
        self.last_usage = None
        self._axis = None
        self._values: list[str] = []

        if client is not None:
            self._client = client
            return

        try:
            import openai
        except ImportError as e:
            raise ProviderError(
                "provider 'openai' requires the 'openai' package - "
                "install it with `pip install regradar[llm]` or `pip install openai`"
            ) from e

        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise ProviderError("OPENAI_API_KEY is not set")
        self._client = openai.OpenAI(api_key=key)

    def configure(self, axis: str, values: list[str], cardinality: str) -> None:
        self._axis = axis
        self._values = values

    def classify(self, prompt: str) -> str:
        response = self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "classification", "schema": _build_schema(self._values), "strict": True},
            },
        )
        usage = getattr(response, "usage", None)
        if usage is not None:
            self.last_usage = {
                "input_tokens": getattr(usage, "prompt_tokens", 0),
                "output_tokens": getattr(usage, "completion_tokens", 0),
            }
        content = response.choices[0].message.content
        payload = json.loads(content)
        payload["axis"] = self._axis
        return json.dumps(payload)

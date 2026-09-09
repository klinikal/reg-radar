"""Ollama provider for `regradar classify` (docs/issues/12-batch-llm
-classifier-runner.md): calls a local Ollama server's HTTP API. `httpx` is
already a project dependency (see collector's HTTP adapters), so nothing
here is optional/lazy the way the anthropic/openai providers are.

No structured-output enforcement - relies on strict prompt instructions
plus the runner's shared JSON parse-and-validate path (issue design note:
"ollama and command share the same parse-and-validate path"). Always
`last_usage = None`: ollama is local/unmetered, and the runner never
fabricates a cost for it (task ruling).
"""
import os

import httpx

_DEFAULT_BASE_URL = "http://localhost:11434"
_DEFAULT_TIMEOUT = 120.0


class OllamaProvider:
    name = "ollama"

    def __init__(self, model: str, base_url: str | None = None, client=None):
        self.model = model
        self.last_usage = None
        self._axis = None
        self._client = client or httpx.Client(
            base_url=base_url or os.environ.get("OLLAMA_BASE_URL", _DEFAULT_BASE_URL),
            timeout=_DEFAULT_TIMEOUT,
        )

    def configure(self, axis: str, values: list[str], cardinality: str) -> None:
        self._axis = axis

    def classify(self, prompt: str) -> str:
        response = self._client.post("/api/generate", json={"model": self.model, "prompt": prompt, "stream": False})
        response.raise_for_status()
        return response.json().get("response", "")

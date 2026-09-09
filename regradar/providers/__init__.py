"""Provider registry for `regradar classify` (docs/issues/12-batch-llm
-classifier-runner.md). Each provider module lazily imports its SDK (or,
for `command`, has no external dependency at all) so the rest of the CLI
works without every provider's extra installed - only picking that
provider requires it.

Provider protocol (issue's design note, one method so a fifth provider is
one file):

    class Provider(Protocol):
        name: str
        model: str
        def classify(self, prompt: str) -> str: ...  # raw model output

Providers also carry a `last_usage` attribute, set after each `classify()`
call: `{"input_tokens": int, "output_tokens": int}` for a metered API
provider, or `None` for an unmetered one (ollama, command) - the runner
never fabricates a cost for `None`.
"""
import logging

logger = logging.getLogger(__name__)


class ProviderError(ValueError):
    """Raised for a provider configuration problem (missing SDK, missing
    API key, missing --command) - a startup error, not a per-call one."""


# name -> {model, rpm} sensible defaults. Model ids and RPM ceilings both
# age as vendors ship new models/plans - # ASSUMPTION: verify these are
# still current before relying on them unattended.
PROVIDER_DEFAULTS = {
    "anthropic": {"model": "claude-sonnet-4-5-20250929", "rpm": 50},
    "openai": {"model": "gpt-5-mini", "rpm": 60},
    "ollama": {"model": "llama3.1", "rpm": 120},
    "command": {"model": None, "rpm": 20},
}


def get_provider(name: str, model: str | None = None, command: str | None = None,
                  rpm: int | None = None, **kwargs):
    """Instantiates the named provider, resolving `model` and `rpm`
    against PROVIDER_DEFAULTS when not explicitly given. Raises
    ProviderError for an unknown provider name or missing requirements
    (e.g. `command` without --command)."""
    if name not in PROVIDER_DEFAULTS:
        raise ProviderError(f"unknown provider {name!r} - choose one of {sorted(PROVIDER_DEFAULTS)}")

    defaults = PROVIDER_DEFAULTS[name]
    resolved_model = model or defaults["model"]
    resolved_rpm = rpm or defaults["rpm"]

    if name == "anthropic":
        from regradar.providers.anthropic import AnthropicProvider
        provider = AnthropicProvider(model=resolved_model, **kwargs)
    elif name == "openai":
        from regradar.providers.openai import OpenAIProvider
        provider = OpenAIProvider(model=resolved_model, **kwargs)
    elif name == "ollama":
        from regradar.providers.ollama import OllamaProvider
        provider = OllamaProvider(model=resolved_model, **kwargs)
    elif name == "command":
        if not command:
            raise ProviderError("provider 'command' requires --command")
        from regradar.providers.command import CommandProvider
        provider = CommandProvider(command=command, model=resolved_model, **kwargs)
    else:  # pragma: no cover - guarded above
        raise ProviderError(f"unknown provider {name!r}")

    provider.rpm = resolved_rpm
    return provider

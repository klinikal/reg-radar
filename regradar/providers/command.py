"""`command` provider for `regradar classify` (docs/issues/12-batch-llm
-classifier-runner.md): shells out to any CLI that reads a prompt on
stdin and writes text on stdout, so a subscription-backed tool (e.g. a
`claude -p`-style invocation) works with no API key and no provider-
specific code. No external dependency - stdlib `subprocess` only.

Always `last_usage = None`: there is no generic way to meter an arbitrary
shelled-out command, and the runner never fabricates a cost (task ruling).
Operators must check their own tool's terms of service before using it for
unattended, scheduled classification - this module makes no vendor
recommendation (task brief ruling).
"""
import shlex
import subprocess
from pathlib import Path

from regradar.providers import ProviderError

_DEFAULT_TIMEOUT = 120.0


class CommandProvider:
    name = "command"

    def __init__(self, command: str, model: str | None = None, timeout: float = _DEFAULT_TIMEOUT):
        self.command = command
        # `by=llm:command/<name>` needs *some* model-like identifier; default
        # to the invoked binary's name so it's not just "None" when the
        # operator doesn't pass --model.
        self.model = model or Path(shlex.split(command)[0]).name
        self.last_usage = None
        self.timeout = timeout
        self._axis = None

    def configure(self, axis: str, values: list[str], cardinality: str) -> None:
        self._axis = axis

    def classify(self, prompt: str) -> str:
        try:
            result = subprocess.run(
                shlex.split(self.command), input=prompt, capture_output=True, text=True, timeout=self.timeout,
            )
        except FileNotFoundError as e:
            raise ProviderError(f"command provider: {e}") from e
        except subprocess.TimeoutExpired as e:
            raise ProviderError(f"command provider timed out after {self.timeout}s") from e
        if result.returncode != 0:
            raise RuntimeError(f"command provider exited {result.returncode}: {result.stderr.strip()[:500]}")
        return result.stdout

"""Tests for `reg-radar classify` (docs/issues/12-batch-llm-classifier
-runner.md): validation, parsing, prompt building, rate limiting, cost
logging, each provider (mocked HTTP / injected client / a stub script for
`command`), and a full CLI run wired through regradar/cli.py."""
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text as sa_text

from collector.schema import Item, make_item_id
from collector.storage import init_db, insert_item
from regradar.classify import (
    RateLimiter,
    build_prompt,
    load_prices,
    log_usage,
    parse_response,
    validate_response,
)
from regradar.cli import main
from regradar.providers import ProviderError, get_provider
from regradar.providers.anthropic import AnthropicProvider
from regradar.providers.command import CommandProvider
from regradar.providers.ollama import OllamaProvider
from regradar.providers.openai import OpenAIProvider
from regradar.taxonomy import Axis, AxisValue, Taxonomy

STUB_SCRIPT = Path(__file__).parent / "fixtures" / "classification" / "stub_provider.py"


def _make_taxonomy():
    document_type = Axis(
        id="document_type", cardinality="one",
        values={
            "warning": AxisValue(id="warning", name="Warning"),
            "press_release": AxisValue(id="press_release", name="Press release"),
        },
    )
    instrument = Axis(
        id="instrument", cardinality="many",
        values={
            "mica": AxisValue(id="mica", name="MiCA", aliases=["Markets in Crypto-Assets"]),
            "dora": AxisValue(id="dora", name="DORA"),
        },
    )
    return Taxonomy(1, {"document_type": document_type, "instrument": instrument}, {})


# --- validate_response() ----------------------------------------------------

def test_validate_response_accepts_a_single_good_value():
    taxonomy = _make_taxonomy()
    parsed = {"axis": "document_type", "values": [{"value": "warning", "confidence": 0.9}]}
    ok, reason, values = validate_response(parsed, "document_type", taxonomy)
    assert ok is True
    assert reason is None
    assert values == [("warning", 0.9)]


def test_validate_response_accepts_empty_values_as_a_confident_no_op():
    taxonomy = _make_taxonomy()
    parsed = {"axis": "instrument", "values": []}
    ok, reason, values = validate_response(parsed, "instrument", taxonomy)
    assert ok is True
    assert values == []


def test_validate_response_rejects_axis_mismatch():
    taxonomy = _make_taxonomy()
    parsed = {"axis": "instrument", "values": [{"value": "mica", "confidence": 0.9}]}
    ok, reason, values = validate_response(parsed, "document_type", taxonomy)
    assert ok is False
    assert "does not match" in reason


def test_validate_response_rejects_unknown_value():
    taxonomy = _make_taxonomy()
    parsed = {"axis": "document_type", "values": [{"value": "not_a_real_value", "confidence": 0.9}]}
    ok, reason, values = validate_response(parsed, "document_type", taxonomy)
    assert ok is False
    assert "unknown value" in reason


def test_validate_response_rejects_cardinality_violation():
    taxonomy = _make_taxonomy()
    parsed = {"axis": "document_type", "values": [
        {"value": "warning", "confidence": 0.9}, {"value": "press_release", "confidence": 0.8},
    ]}
    ok, reason, values = validate_response(parsed, "document_type", taxonomy)
    assert ok is False
    assert "single-valued" in reason


def test_validate_response_rejects_out_of_range_confidence():
    taxonomy = _make_taxonomy()
    parsed = {"axis": "instrument", "values": [{"value": "mica", "confidence": 1.5}]}
    ok, reason, values = validate_response(parsed, "instrument", taxonomy)
    assert ok is False
    assert "out of range" in reason


def test_validate_response_rejects_non_dict():
    taxonomy = _make_taxonomy()
    ok, reason, values = validate_response(None, "document_type", taxonomy)
    assert ok is False
    assert "not a JSON object" in reason


# --- parse_response() -------------------------------------------------------

def test_parse_response_extracts_clean_json():
    assert parse_response('{"axis": "document_type", "values": []}') == {"axis": "document_type", "values": []}


def test_parse_response_extracts_json_from_fenced_and_noisy_text():
    raw = 'Sure, here is the classification:\n```json\n{"axis": "document_type", "values": []}\n```\nDone.'
    assert parse_response(raw) == {"axis": "document_type", "values": []}


def test_parse_response_returns_none_for_no_json():
    assert parse_response("I cannot help with that.") is None


def test_parse_response_returns_none_for_empty_string():
    assert parse_response("") is None


# --- build_prompt() ----------------------------------------------------------

def test_build_prompt_includes_axis_values_and_aliases():
    taxonomy_dict = _make_taxonomy().to_dict()
    prompt = build_prompt(taxonomy_dict, "instrument", {"title": "A notice", "body_md": "About MiCA."})
    assert '"instrument"' in prompt
    assert "mica (MiCA)" in prompt
    assert "Markets in Crypto-Assets" in prompt
    assert "multi-valued" in prompt
    assert "A notice" in prompt


def test_build_prompt_notes_single_valued_axis():
    taxonomy_dict = _make_taxonomy().to_dict()
    prompt = build_prompt(taxonomy_dict, "document_type", {"title": "x", "body_md": ""})
    assert "single-valued" in prompt


# --- RateLimiter (injectable clock/sleep - never sleeps in tests) ----------

def test_rate_limiter_sleeps_the_remaining_interval():
    # wait() reads the clock once per call, plus once more if it sleeps (to
    # reset _last_call to the post-sleep time): call 1 -> 0.0 (no sleep,
    # first call ever); call 2 -> 0.6 (0.6s elapsed of a 1.0s/60rpm
    # interval, so it sleeps the remaining ~0.4s), then 1.0 post-sleep.
    clock_values = iter([0.0, 0.6, 1.0])
    sleeps = []
    limiter = RateLimiter(rpm=60, sleep_fn=sleeps.append, clock=lambda: next(clock_values))
    limiter.wait()
    limiter.wait()
    assert sleeps == [pytest.approx(0.4)]


def test_rate_limiter_does_not_sleep_once_enough_time_has_passed():
    clock_values = iter([0.0, 2.0])
    sleeps = []
    limiter = RateLimiter(rpm=60, sleep_fn=sleeps.append, clock=lambda: next(clock_values))
    limiter.wait()
    limiter.wait()
    assert sleeps == []


def test_rate_limiter_with_rpm_zero_never_sleeps():
    sleeps = []
    limiter = RateLimiter(rpm=0, sleep_fn=sleeps.append, clock=lambda: 0.0)
    limiter.wait()
    limiter.wait()
    assert sleeps == []


# --- cost logging ------------------------------------------------------------

def test_log_usage_unmetered(caplog):
    with caplog.at_level(logging.INFO):
        log_usage("ollama", "llama3.1", None, {})
    assert "unmetered" in caplog.text


def test_log_usage_computes_cost_from_price_table(caplog):
    prices = {"anthropic": {"claude-x": {"input_per_million": 3.0, "output_per_million": 15.0}}}
    with caplog.at_level(logging.INFO):
        log_usage("anthropic", "claude-x", {"input_tokens": 1_000_000, "output_tokens": 500_000}, prices)
    assert "$10.5000" in caplog.text


def test_log_usage_reports_unknown_cost_without_a_price_entry(caplog):
    with caplog.at_level(logging.INFO):
        log_usage("anthropic", "claude-x", {"input_tokens": 100, "output_tokens": 50}, {})
    assert "cost unknown" in caplog.text


def test_load_prices_missing_file_returns_empty_dict(tmp_path):
    assert load_prices(str(tmp_path / "nope.yaml")) == {}


# --- providers: anthropic (injected client, no real SDK/network needed) ----

class _FakeAnthropicUsage:
    def __init__(self, input_tokens, output_tokens):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeAnthropicBlock:
    def __init__(self, input_):
        self.type = "tool_use"
        self.input = input_


class _FakeAnthropicResponse:
    def __init__(self, values):
        self.usage = _FakeAnthropicUsage(120, 40)
        self.content = [_FakeAnthropicBlock({"values": values, "reasoning": "stub"})]


class _FakeAnthropicMessages:
    def __init__(self, values):
        self._values = values
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeAnthropicResponse(self._values)


class _FakeAnthropicClient:
    def __init__(self, values):
        self.messages = _FakeAnthropicMessages(values)


def test_anthropic_provider_classify_returns_validatable_json():
    client = _FakeAnthropicClient([{"value": "warning", "confidence": 0.9}])
    provider = AnthropicProvider(model="claude-test", client=client)
    provider.configure("document_type", ["warning", "press_release"], "one")

    raw = provider.classify("classify this")

    parsed = json.loads(raw)
    assert parsed == {"axis": "document_type", "values": [{"value": "warning", "confidence": 0.9}], "reasoning": "stub"}
    assert provider.last_usage == {"input_tokens": 120, "output_tokens": 40}
    assert client.messages.last_kwargs["tool_choice"] == {"type": "tool", "name": "submit_classification"}


def test_anthropic_provider_requires_api_key_without_injected_client(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    pytest.importorskip("anthropic")
    with pytest.raises(ProviderError, match="ANTHROPIC_API_KEY"):
        AnthropicProvider(model="claude-test")


def test_anthropic_provider_missing_sdk_raises_provider_error_not_import_error(monkeypatch):
    # `import anthropic` inside AnthropicProvider.__init__ must surface as a
    # ProviderError (a startup config problem), not propagate the raw
    # ImportError - regardless of whether the SDK happens to be installed in
    # the environment running the test. Setting sys.modules[name] = None is
    # the standard trick to force `import anthropic` to raise ImportError.
    monkeypatch.setitem(sys.modules, "anthropic", None)
    with pytest.raises(ProviderError, match="pip install"):
        AnthropicProvider(model="claude-test")


# --- providers: openai (injected client) ------------------------------------

class _FakeOpenAIUsage:
    def __init__(self):
        self.prompt_tokens = 200
        self.completion_tokens = 30


class _FakeOpenAIMessage:
    def __init__(self, content):
        self.content = content


class _FakeOpenAIChoice:
    def __init__(self, content):
        self.message = _FakeOpenAIMessage(content)


class _FakeOpenAIResponse:
    def __init__(self, content):
        self.usage = _FakeOpenAIUsage()
        self.choices = [_FakeOpenAIChoice(content)]


class _FakeOpenAICompletions:
    def __init__(self, content):
        self._content = content
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeOpenAIResponse(self._content)


class _FakeOpenAIChat:
    def __init__(self, content):
        self.completions = _FakeOpenAICompletions(content)


class _FakeOpenAIClient:
    def __init__(self, content):
        self.chat = _FakeOpenAIChat(content)


def test_openai_provider_classify_returns_validatable_json():
    content = json.dumps({"values": [{"value": "mica", "confidence": 0.7}], "reasoning": "stub"})
    client = _FakeOpenAIClient(content)
    provider = OpenAIProvider(model="gpt-test", client=client)
    provider.configure("instrument", ["mica", "dora"], "many")

    raw = provider.classify("classify this")

    parsed = json.loads(raw)
    assert parsed == {"values": [{"value": "mica", "confidence": 0.7}], "reasoning": "stub", "axis": "instrument"}
    assert provider.last_usage == {"input_tokens": 200, "output_tokens": 30}
    assert client.chat.completions.last_kwargs["response_format"]["json_schema"]["strict"] is True


# --- providers: ollama (httpx.MockTransport, no real network) --------------

def test_ollama_provider_classify_hits_generate_endpoint_and_is_unmetered():
    def handler(request):
        body = json.loads(request.content)
        assert body["model"] == "llama-test"
        assert body["stream"] is False
        return httpx.Response(200, json={"response": '{"axis": "document_type", "values": []}'})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://fake-ollama")
    provider = OllamaProvider(model="llama-test", client=client)
    provider.configure("document_type", ["warning"], "one")

    raw = provider.classify("classify this")

    assert json.loads(raw) == {"axis": "document_type", "values": []}
    assert provider.last_usage is None  # ollama is always unmetered


# --- providers: command (stub script, no network/SDK at all) ---------------

def test_command_provider_runs_stub_script_and_is_unmetered():
    provider = CommandProvider(command=f"{sys.executable} {STUB_SCRIPT}", model="stub")
    import os
    os.environ["CLASSIFY_STUB_MODE"] = "valid"
    try:
        raw = provider.classify('classifying a regulatory document on the "document_type" taxonomy axis')
    finally:
        os.environ.pop("CLASSIFY_STUB_MODE", None)
    parsed = json.loads(raw)
    assert parsed["axis"] == "document_type"
    assert parsed["values"] == [{"value": "press_release", "confidence": 0.8}]
    assert provider.last_usage is None
    assert provider.model == "stub"


def test_command_provider_defaults_model_to_binary_name():
    provider = CommandProvider(command="true")
    assert provider.model == "true"


def test_command_provider_raises_runtime_error_on_nonzero_exit():
    provider = CommandProvider(command="false")
    with pytest.raises(RuntimeError):
        provider.classify("anything")


# --- get_provider() factory --------------------------------------------------

def test_get_provider_unknown_name_raises():
    with pytest.raises(ProviderError, match="unknown provider"):
        get_provider("not-a-provider")


def test_get_provider_command_without_command_arg_raises():
    with pytest.raises(ProviderError, match="requires --command"):
        get_provider("command")


def test_get_provider_command_resolves_rpm_and_model_defaults():
    provider = get_provider("command", command="true")
    assert provider.rpm == 20
    assert provider.model == "true"


# --- full CLI run against the `command` provider (acceptance criteria) -----

def _insert_items(engine, n):
    now = datetime.now(timezone.utc)
    items = []
    for i in range(n):
        item = Item(
            id=make_item_id("esma", f"https://example.com/{i}", now), regulator_id="esma",
            source_label="news", title=f"Notice {i}", url=f"https://example.com/{i}",
            published_at=now, fetched_at=now, date_inferred=False, language="en",
            content_snippet="...", source_type="rss", body_md=f"Body of notice {i}.",
        )
        insert_item(engine, item)
        items.append(item)
    return items


def _classification_rows(engine, item_ids):
    # Each test uses a fresh tmp_path database, so this is every row in it.
    with engine.connect() as conn:
        return conn.execute(
            sa_text("SELECT item_id, axis, value, by, confidence, classifier_id FROM classifications"),
        ).fetchall()


def test_classify_command_provider_end_to_end_writes_via_write_classification(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    engine = init_db(db_path)
    items = _insert_items(engine, 3)
    monkeypatch.setenv("CLASSIFY_STUB_MODE", "valid")

    exit_code = main([
        "classify", "--provider", "command", "--command", f"{sys.executable} {STUB_SCRIPT}",
        "--axis", "document_type", "--limit", "3", "--model", "stub", "--rpm", "0",
        "--db-path", db_path,
        "--taxonomy-path", "config/taxonomy.yaml",
        "--jurisdictions-path", "config/jurisdictions.yaml",
    ])
    assert exit_code == 0

    rows = _classification_rows(engine, [i.id for i in items])
    assert len(rows) == 3
    for item_id, axis, value, by, confidence, classifier_id in rows:
        assert axis == "document_type"
        assert value == "press_release"
        assert by == "llm:command/stub"
        assert confidence == 0.8
        assert classifier_id == "reg-radar-classify/command"


def test_classify_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    db_path = str(tmp_path / "test.db")
    engine = init_db(db_path)
    items = _insert_items(engine, 2)
    monkeypatch.setenv("CLASSIFY_STUB_MODE", "valid")

    exit_code = main([
        "classify", "--provider", "command", "--command", f"{sys.executable} {STUB_SCRIPT}",
        "--axis", "document_type", "--limit", "2", "--model", "stub", "--rpm", "0", "--dry-run",
        "--db-path", db_path,
        "--taxonomy-path", "config/taxonomy.yaml",
        "--jurisdictions-path", "config/jurisdictions.yaml",
    ])
    assert exit_code == 0
    assert "[DRY RUN]" in capsys.readouterr().out
    assert _classification_rows(engine, [i.id for i in items]) == []


@pytest.mark.parametrize("mode", ["bad_axis", "bad_value", "bad_cardinality", "bad_confidence"])
def test_classify_rejects_and_skips_invalid_responses(tmp_path, monkeypatch, caplog, mode):
    db_path = str(tmp_path / "test.db")
    engine = init_db(db_path)
    items = _insert_items(engine, 1)
    monkeypatch.setenv("CLASSIFY_STUB_MODE", mode)

    with caplog.at_level(logging.WARNING):
        exit_code = main([
            "classify", "--provider", "command", "--command", f"{sys.executable} {STUB_SCRIPT}",
            "--axis", "document_type", "--limit", "1", "--model", "stub", "--rpm", "0",
            "--db-path", db_path,
            "--taxonomy-path", "config/taxonomy.yaml",
            "--jurisdictions-path", "config/jurisdictions.yaml",
        ])

    assert exit_code == 0
    assert _classification_rows(engine, [i.id for i in items]) == []
    assert "rejected response" in caplog.text


def test_classify_help_lists_classify_subcommand(capsys):
    exit_code = None
    try:
        main(["--help"])
    except SystemExit as e:
        exit_code = e.code
    assert exit_code == 0
    assert "classify" in capsys.readouterr().out

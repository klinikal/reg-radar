"""`regradar classify --provider anthropic|openai|ollama|command [--axis
<a>] [--limit <n>] [--dry-run]` (see ../wiki/content/cli-classify.md).

The unattended counterpart to skills/regradar-classifier/SKILL.md: same
backlog (`get_unclassified`), same taxonomy (`Taxonomy.to_dict()` - the
same rendering `get_taxonomy_tool` hands an MCP-connected agent, so the two
paths can't drift on what's valid), same write path
(`write_classification`, `by=llm:<provider>/<model>`) - just driven by a
provider under regradar/providers/ instead of an agent reading the skill.

Wired into regradar/cli.py's subcommand registry (add_subparser()/run() -
see that module's docstring for the convention). Also runnable standalone
via `python -m regradar.classify classify ...`.

One axis per model call (issue design note): a response is validated
against the taxonomy - known axis, known value ids, cardinality, confidence
in range - before anything is written; a response failing validation is
logged with the reason and skipped, never written as a low-confidence
guess (task brief ruling: "Failed validations are logged, not
quarantined").
"""
import argparse
import json
import logging
import os
import sys
import time

import yaml

from collector.storage import init_db
from mcpserver.queries import get_unclassified
from regradar.classification import write_classification
from regradar.providers import PROVIDER_DEFAULTS, ProviderError, get_provider
from regradar.taxonomy import build_taxonomy

logger = logging.getLogger(__name__)

DEFAULT_PRICES_PATH = "config/llm_prices.yaml"
_MAX_BODY_CHARS = 4000


class RateLimiter:
    """Enforces a requests-per-minute ceiling between provider calls.
    `sleep_fn`/`clock` are injectable so tests exercise the pacing logic
    without ever actually sleeping (task ruling)."""

    def __init__(self, rpm: int, sleep_fn=time.sleep, clock=time.monotonic):
        self._min_interval = 60.0 / rpm if rpm and rpm > 0 else 0.0
        self._sleep = sleep_fn
        self._clock = clock
        self._last_call = None

    def wait(self) -> None:
        if self._min_interval <= 0:
            return
        now = self._clock()
        if self._last_call is not None:
            remaining = self._min_interval - (now - self._last_call)
            if remaining > 0:
                self._sleep(remaining)
                now = self._clock()
        self._last_call = now


def build_prompt(taxonomy_dict: dict, axis: str, item: dict) -> str:
    """Mirrors skills/regradar-classifier/SKILL.md's prompt contract (one
    axis, the taxonomy's values/aliases, title + truncated body, a
    confidence per value, prefer leaving it unset over guessing) so the
    unattended runner and the interactive skill can't give a model
    different instructions for the same job."""
    axis_info = next(a for a in taxonomy_dict["axes"] if a["id"] == axis)
    values_desc = "\n".join(
        f"- {v['id']} ({v['name']})" + (f" - aliases: {', '.join(v['aliases'])}" if v["aliases"] else "")
        for v in axis_info["values"]
    )
    cardinality_note = (
        "This axis is single-valued: choose exactly one value, or return an empty \"values\" list if none apply."
        if axis_info["cardinality"] == "one"
        else "This axis is multi-valued: return zero or more values."
    )
    body = (item.get("body_md") or "")[:_MAX_BODY_CHARS]
    return (
        f"You are classifying a regulatory document on the \"{axis}\" taxonomy axis.\n\n"
        f"{cardinality_note}\n\n"
        f"Valid values:\n{values_desc}\n\n"
        f"Document title: {item.get('title') or '(untitled)'}\n"
        f"Document body:\n{body}\n\n"
        "Respond with ONLY a JSON object matching this schema, no other text:\n"
        f'{{"axis": "{axis}", "values": [{{"value": "<value id>", "confidence": <0.0-1.0>}}], '
        '"reasoning": "<one short sentence>"}\n\n'
        "If no value applies confidently, return an empty \"values\" list rather than guessing "
        "below 0.5 confidence."
    )


def parse_response(raw: str) -> dict | None:
    """Extracts and parses a JSON object from raw model output, tolerant of
    a ```-fenced block or leading/trailing prose around it (ollama/command
    have no structured-output guarantee; anthropic/openai's providers
    already hand back clean JSON, which this also parses fine)."""
    if not raw:
        return None
    text = raw.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def validate_response(parsed, axis: str, taxonomy) -> tuple:
    """Validates a parsed response against the taxonomy (issue's design
    notes): axis must be the one requested, cardinality-one axes reject
    more than one value, every value id must exist for the axis, every
    confidence must fall in 0.0-1.0. Returns (ok, reason, values) where
    `values` is a list of (value_id, confidence) tuples to write - a valid
    empty list means "the model confidently found nothing"."""
    if not isinstance(parsed, dict):
        return False, "response is not a JSON object", []
    response_axis = parsed.get("axis", axis)
    if response_axis != axis:
        return False, f"response axis {response_axis!r} does not match requested axis {axis!r}", []
    if axis not in taxonomy.axes():
        return False, f"unknown axis {axis!r}", []

    raw_values = parsed.get("values")
    if not isinstance(raw_values, list):
        return False, "response is missing a 'values' list", []
    if not raw_values:
        return True, None, []

    cardinality = taxonomy.cardinality(axis)
    if cardinality == "one" and len(raw_values) > 1:
        return False, f"axis {axis!r} is single-valued but response has {len(raw_values)} values", []

    values = []
    for entry in raw_values:
        if not isinstance(entry, dict) or "value" not in entry or "confidence" not in entry:
            return False, "each value needs 'value' and 'confidence'", []
        value_id = entry["value"]
        if not taxonomy.is_valid(axis, value_id):
            return False, f"unknown value {value_id!r} for axis {axis!r}", []
        try:
            confidence = float(entry["confidence"])
        except (TypeError, ValueError):
            return False, "confidence must be a number", []
        if not (0.0 <= confidence <= 1.0):
            return False, f"confidence {confidence} out of range 0.0-1.0", []
        values.append((value_id, confidence))
    return True, None, values


def load_prices(path: str) -> dict:
    """A missing config/llm_prices.yaml just means costs log as 'unknown'
    for API providers - not an error, matching webhooks.yaml/digests.yaml's
    own opt-in-config precedent elsewhere in this CLI."""
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def log_usage(provider_name: str, model: str, usage: dict | None, prices: dict) -> None:
    """Logs tokens + an estimated cost for a metered provider (anthropic,
    openai), or 'unmetered' for ollama/command - never fabricating a cost
    figure for the latter (task ruling)."""
    if usage is None:
        logger.info("classify: %s/%s call - unmetered", provider_name, model)
        return
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    price = (prices.get(provider_name) or {}).get(model)
    if not price:
        logger.info(
            "classify: %s/%s call - %d input / %d output tokens, cost unknown (no config/llm_prices.yaml entry)",
            provider_name, model, input_tokens, output_tokens,
        )
        return
    cost = (
        (input_tokens / 1_000_000) * price.get("input_per_million", 0)
        + (output_tokens / 1_000_000) * price.get("output_per_million", 0)
    )
    logger.info(
        "classify: %s/%s call - %d input / %d output tokens, ~$%.4f",
        provider_name, model, input_tokens, output_tokens, cost,
    )


def run(args) -> int:
    engine = init_db(args.db_path)
    taxonomy = build_taxonomy(args.taxonomy_path, args.jurisdictions_path)

    if args.axis and args.axis not in taxonomy.axes():
        logger.error("Unknown axis %r - valid axes: %s", args.axis, sorted(taxonomy.axes()))
        return 1
    axes = [args.axis] if args.axis else taxonomy.axes()

    try:
        provider = get_provider(args.provider, model=args.model, command=args.command, rpm=args.rpm)
    except ProviderError as e:
        logger.error("classify: %s", e)
        return 1

    prices = load_prices(args.prices_path)
    limiter = RateLimiter(provider.rpm)
    taxonomy_dict = taxonomy.to_dict()
    classifier_id = f"reg-radar-classify/{args.provider}"

    written = 0
    skipped = 0
    for axis in axes:
        items = get_unclassified(engine, axis, limit=args.limit)
        axis_values = [v.id for v in taxonomy.axis_values(axis)]
        cardinality = taxonomy.cardinality(axis)

        for item in items:
            if hasattr(provider, "configure"):
                provider.configure(axis, axis_values, cardinality)

            limiter.wait()
            try:
                raw = provider.classify(build_prompt(taxonomy_dict, axis, item))
            except Exception as e:
                logger.warning("classify: provider call failed for item %s axis %r: %s", item["id"], axis, e)
                skipped += 1
                continue

            log_usage(args.provider, provider.model, getattr(provider, "last_usage", None), prices)

            parsed = parse_response(raw)
            ok, reason, values = validate_response(parsed, axis, taxonomy)
            if not ok:
                logger.warning("classify: rejected response for item %s axis %r: %s", item["id"], axis, reason)
                skipped += 1
                continue
            if not values:
                logger.info("classify: item %s axis %r - model found no confident value, leaving unset",
                             item["id"], axis)
                continue

            by = f"llm:{args.provider}/{provider.model}"
            if args.dry_run:
                print(f"[DRY RUN] {item['id']} {axis} -> {values} (by={by})")
                written += len(values)
                continue

            for value, confidence in values:
                write_classification(engine, taxonomy, item["id"], axis, value, by=by,
                                      confidence=confidence, classifier_id=classifier_id)
            written += len(values)

    verb = "[DRY RUN] Would write" if args.dry_run else "Wrote"
    logger.info("classify: %s %d classification(s), skipped %d", verb, written, skipped)
    return 0


def add_subparser(subparsers) -> None:
    parser = subparsers.add_parser("classify", help="Classify unclassified items via a pluggable LLM provider")
    parser.add_argument("--provider", required=True, choices=sorted(PROVIDER_DEFAULTS),
                         help="LLM provider: anthropic, openai, ollama, or command (shells out to any CLI "
                              "that reads a prompt on stdin and writes text on stdout)")
    parser.add_argument("--axis", help="Taxonomy axis to classify (default: every axis)")
    parser.add_argument("--limit", type=int, default=50, help="Max items to classify per axis this run")
    parser.add_argument("--dry-run", action="store_true", help="Classify and print, write nothing")
    parser.add_argument("--model", help="Override the provider's default model id")
    parser.add_argument("--command", help='Shell command for --provider command, e.g. "claude -p"')
    parser.add_argument("--rpm", type=int, help="Override the provider's default requests-per-minute limit")
    parser.add_argument("--db-path", default=os.getenv("DB_PATH", "/app/data/reg_radar.db"))
    parser.add_argument("--taxonomy-path", default=os.getenv("TAXONOMY_PATH", "config/taxonomy.yaml"))
    parser.add_argument("--jurisdictions-path", default=os.getenv("JURISDICTIONS_PATH", "config/jurisdictions.yaml"))
    parser.add_argument("--prices-path", default=os.getenv("LLM_PRICES_PATH", DEFAULT_PRICES_PATH))
    parser.set_defaults(func=run)


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_subparser(subparsers)
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()

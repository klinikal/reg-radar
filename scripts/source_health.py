"""Shared source-health logic: fetching a source exactly as the production
collector does, checking its parser output against the fixture/live-URL
contract, and filing/updating/closing GitHub issues for failures.

Used by both `scripts/capture_fixture.py` (manual fixture (re)capture) and
the nightly `source-health-nightly` workflow (live contract check), so the
fetch path, parse path and assertion are defined exactly once.
"""
import json
import logging
import os
import subprocess
from pathlib import Path

from collector.adapters.base import FetchContext
from collector.adapters.registry import get as get_adapter
from collector.main import load_regulators as _load_regulators_split
from collector.pipeline import process_raw_documents
from collector.schema import Item

logger = logging.getLogger(__name__)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "sources"

# A fixture over this size is left uncaptured (reported, not trimmed -
# trimming would defeat the point of an "unmodified" regression fixture).
MAX_FIXTURE_BYTES = 2 * 1024 * 1024
# Above this, capture still writes the fixture but flags it as worth a
# second look before committing something this large to git.
FIXTURE_WARN_BYTES = 300 * 1024

ISSUE_LABEL = "source-health"


def load_regulators(path: str | None = None) -> list[dict]:
    path = path or os.getenv("REGULATORS_PATH", "config/regulators")
    return _load_regulators_split(path)


def iter_enabled_sources(regulators: list[dict]):
    """Yields (regulator, source) for every enabled source of every enabled
    regulator - `enabled` defaults to True at both levels, matching
    collector.main.run_collection's own checks."""
    for reg in regulators:
        if not reg.get("enabled", True):
            continue
        for source in reg.get("sources", []):
            if not source.get("enabled", True):
                continue
            yield reg, source


def fixture_extension(source_type: str) -> str:
    if source_type in ("rss", "atom", "sitemap"):
        return "xml"
    if source_type == "json_api":
        return "json"
    return "html"


def fixture_path(regulator_id: str, source: dict) -> Path:
    ext = fixture_extension(source.get("type", "rss"))
    return FIXTURES_DIR / regulator_id / f"{source['label']}.{ext}"


def fetch_raw(regulator: dict, source: dict, cfg: dict) -> bytes:
    """Fetches a source's raw response through its registered adapter's own
    HTTP fetch (collector/adapters/<type>.py), but returns the raw bytes
    instead of parsed RawDocuments - fixture capture and the nightly live
    check both need the response itself, not just what the parser makes of
    it."""
    source_type = source.get("type", "rss")
    adapter = get_adapter(source_type)
    if adapter is None:
        raise ValueError(f"unknown source type '{source_type}'")

    ctx = FetchContext(
        user_agent=cfg.get("user_agent", "regradar/2.0"),
        timeout=int(cfg.get("timeout", 30)),
        retries=int(cfg.get("retries", 3)),
        flaresolverr_url=cfg.get("flaresolverr_url") if regulator.get("cloudflare_protected", False) else None,
        regulator_id=regulator["id"],
    )
    content = adapter.fetch_raw(source, ctx)
    return content.encode("utf-8") if isinstance(content, str) else content


def parse_fixture(source: dict, regulator_id: str, content: bytes, fetch_pdf: bool = True) -> list[Item]:
    """Runs the registered adapter's parse() against raw fixture/live
    content, then the shared pipeline - the same code path the collector
    itself uses (docs/issues/08-source-adapter-framework-registry-split.md).

    fetch_pdf=False forces off a scrape source's extract_pdf_text - that
    option makes the scrape adapter fetch each matched item's URL over the
    network, which the offline fixture contract test must never do. The
    live nightly check (check_source) wants the real, network-fetching
    behaviour, so it leaves this at the default True.
    """
    source_type = source.get("type", "rss")
    adapter = get_adapter(source_type)
    if adapter is None:
        raise ValueError(f"unknown source type '{source_type}'")

    if source_type in ("rss", "atom"):
        raw_docs = adapter.parse(content, source, regulator_id)
    else:
        text_content = content.decode("utf-8", errors="replace")
        if source_type == "scrape" and not fetch_pdf and source.get("scrape", {}).get("extract_pdf_text"):
            source = {**source, "scrape": {**source["scrape"], "extract_pdf_text": False}}
        if source_type == "scrape":
            raw_docs = adapter.parse(text_content, source, regulator_id, {})
        else:
            raw_docs = adapter.parse(text_content, source, regulator_id)
    return process_raw_documents(raw_docs, source, regulator_id)


def assert_contract(items: list[Item]) -> None:
    """Raises AssertionError describing what's missing unless at least one
    parsed item has a non-empty title, a non-empty url, and a resolved date
    state (published_at set, or date_inferred True)."""
    if not items:
        raise AssertionError("parser returned 0 items")
    for item in items:
        if item.title and item.url and (item.published_at is not None or item.date_inferred):
            return
    raise AssertionError(
        "no parsed item had a non-empty title, non-empty url and a resolved date state "
        "(published_at set or date_inferred True)"
    )


def capture(regulator: dict, source: dict, cfg: dict, dry_run: bool = False) -> tuple[Path, int]:
    """Fetches a source live and writes its raw response to the fixture
    path, unmodified. Raises if the response exceeds MAX_FIXTURE_BYTES -
    that's reported as "not captured", never silently trimmed."""
    content = fetch_raw(regulator, source, cfg)
    if len(content) > MAX_FIXTURE_BYTES:
        raise ValueError(f"{len(content)} bytes exceeds the {MAX_FIXTURE_BYTES} byte fixture cap")
    path = fixture_path(regulator["id"], source)
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return path, len(content)


def check_source(regulator: dict, source: dict, cfg: dict) -> str | None:
    """Live equivalent of the offline contract test: fetches the source and
    runs the same assertion the fixture test does. Returns None on success,
    else a string describing the failure."""
    try:
        content = fetch_raw(regulator, source, cfg)
        items = parse_fixture(source, regulator["id"], content)
        assert_contract(items)
    except Exception as e:
        return str(e)
    return None


def issue_title(regulator_id: str, source_label: str) -> str:
    return f"source-health: {regulator_id}/{source_label} failing"


def _gh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True)


def find_open_issue(regulator_id: str, source_label: str) -> str | None:
    """Returns the issue number of the open source-health issue for this
    source, if one already exists."""
    title = issue_title(regulator_id, source_label)
    result = _gh(
        "issue", "list", "--label", ISSUE_LABEL, "--state", "open",
        "--search", title, "--json", "number,title",
    )
    for issue in json.loads(result.stdout or "[]"):
        if issue.get("title") == title:
            return str(issue["number"])
    return None


def open_or_update_issue(regulator_id: str, source_label: str, error: str, diff: str = "") -> None:
    title = issue_title(regulator_id, source_label)
    body = f"Source `{regulator_id}/{source_label}` failed its nightly health check.\n\nError: {error}\n"
    if diff:
        body += f"\nDiff against last-known-good fixture:\n```\n{diff}\n```\n"
    number = find_open_issue(regulator_id, source_label)
    if number:
        _gh("issue", "comment", number, "--body", body)
    else:
        _gh("issue", "create", "--title", title, "--label", ISSUE_LABEL, "--body", body)


def close_issue_if_open(regulator_id: str, source_label: str) -> None:
    number = find_open_issue(regulator_id, source_label)
    if number:
        _gh("issue", "close", number, "--comment", "Source recovered on the latest nightly check - closing automatically.")


def run_all_checks(regulators: list[dict], cfg: dict, file_issues: bool = True) -> dict[str, str | None]:
    """Runs check_source() for every enabled source and, when file_issues is
    True, opens/updates an issue per failure and closes any that recovered.
    Returns {"regulator_id/source_label": error_or_None}."""
    results: dict[str, str | None] = {}
    for regulator, source in iter_enabled_sources(regulators):
        key = f"{regulator['id']}/{source['label']}"
        error = check_source(regulator, source, cfg)
        results[key] = error
        if not file_issues:
            continue
        if error:
            open_or_update_issue(regulator["id"], source["label"], error)
        else:
            close_issue_if_open(regulator["id"], source["label"])
    return results


def main(argv=None) -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from collector.main import load_config

    regulators = load_regulators()
    cfg = load_config()
    results = run_all_checks(regulators, cfg)

    failures = {k: v for k, v in results.items() if v}
    logger.info("source health: %d checked, %d failed", len(results), len(failures))
    for key, error in failures.items():
        logger.error("  FAILED %s: %s", key, error)
    return 1 if failures else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

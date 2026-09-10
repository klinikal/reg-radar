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
from datetime import datetime, timezone
from pathlib import Path

from collector.adapters.base import FetchContext
from collector.adapters.registry import get as get_adapter
from collector.main import load_regulators as _load_regulators_split
from collector.pipeline import process_raw_documents
from collector.schema import Item
from collector.adapters.http import DEFAULT_USER_AGENT

logger = logging.getLogger(__name__)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "sources"

# A fixture over this size is left uncaptured (reported, not trimmed -
# trimming would defeat the point of an "unmodified" regression fixture).
MAX_FIXTURE_BYTES = 2 * 1024 * 1024
# Above this, capture still writes the fixture but flags it as worth a
# second look before committing something this large to git.
FIXTURE_WARN_BYTES = 300 * 1024

ISSUE_LABEL = "source-health"

# Staleness assertion (issue #39 A). A feed can be well-formed, return 200
# and parse cleanly while its newest entry is years old - FINRA's rss.xml
# serves ten items whose newest is from July 2020, and Texas DOB's serves a
# valid RSS envelope with no <item> at all. HTTP status catches neither, and
# to an operator both look exactly like a quiet regulator.
STALENESS_WARN_DAYS = int(os.getenv("STALENESS_WARN_DAYS", "90"))
STALENESS_ERROR_DAYS = int(os.getenv("STALENESS_ERROR_DAYS", "365"))


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
        user_agent=cfg.get("user_agent", DEFAULT_USER_AGENT),
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


def staleness_thresholds(source: dict) -> tuple[int, int]:
    """(warn_days, error_days) for `source`, honouring a per-source
    `staleness:` override in config/regulators/*.yaml (documented in
    _template.yaml). The override exists for genuinely low-cadence sources -
    the FATF grey list fires roughly three times a year, so the 90-day
    default would be pure noise there."""
    cfg = source.get("staleness") or {}
    return (
        int(cfg.get("warn_days", STALENESS_WARN_DAYS)),
        int(cfg.get("error_days", STALENESS_ERROR_DAYS)),
    )


def newest_published_at(items: list[Item]) -> datetime | None:
    """The newest real publication date across `items`, or None if not one
    item carries one. An inferred date is the collector's own fetch-time
    guess, not the source's claim, so it says nothing about staleness."""
    dates = [
        (i.published_at if i.published_at.tzinfo else i.published_at.replace(tzinfo=timezone.utc))
        for i in items if i.published_at is not None
    ]
    return max(dates) if dates else None


def check_staleness(items: list[Item], source: dict, now: datetime | None = None) -> tuple[str | None, str | None]:
    """Returns (error, warning), either or both None. No date to judge from
    means no assertion: silence is better than a false alarm on a source
    whose dates are all inferred."""
    newest = newest_published_at(items)
    if newest is None:
        return None, None
    now = now or datetime.now(timezone.utc)
    age_days = (now - newest).days
    warn_days, error_days = staleness_thresholds(source)
    detail = f"newest item is {age_days} days old ({newest.date().isoformat()})"
    if age_days >= error_days:
        return f"stale: {detail}, over the {error_days}-day error threshold", None
    if age_days >= warn_days:
        return None, f"stale: {detail}, over the {warn_days}-day warn threshold"
    return None, None


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


def check_source(regulator: dict, source: dict, cfg: dict, warnings: list | None = None) -> str | None:
    """Live equivalent of the offline contract test: fetches the source and
    runs the same assertions the fixture test does, plus the staleness
    assertion. Returns None on success, else a string describing the
    failure.

    A staleness *warning* is not a failure - it would file an issue for
    every genuinely quiet regulator - so it is appended to `warnings`
    (if a list was passed) rather than returned."""
    try:
        content = fetch_raw(regulator, source, cfg)
        items = parse_fixture(source, regulator["id"], content)
        assert_contract(items)
    except Exception as e:
        return str(e)
    error, warning = check_staleness(items, source)
    if warning is not None and warnings is not None:
        warnings.append(warning)
    return error


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


def run_all_checks(regulators: list[dict], cfg: dict, file_issues: bool = True,
                    warnings: dict | None = None) -> dict[str, str | None]:
    """Runs check_source() for every enabled source and, when file_issues is
    True, opens/updates an issue per failure and closes any that recovered.
    Returns {"regulator_id/source_label": error_or_None}.

    Pass a dict as `warnings` to also collect non-failing staleness
    warnings, keyed the same way."""
    results: dict[str, str | None] = {}
    for regulator, source in iter_enabled_sources(regulators):
        key = f"{regulator['id']}/{source['label']}"
        source_warnings: list[str] = []
        error = check_source(regulator, source, cfg, warnings=source_warnings)
        results[key] = error
        if warnings is not None and source_warnings:
            warnings[key] = source_warnings
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
    warnings: dict[str, list[str]] = {}
    results = run_all_checks(regulators, cfg, warnings=warnings)

    failures = {k: v for k, v in results.items() if v}
    logger.info(
        "source health: %d checked, %d failed, %d warned",
        len(results), len(failures), len(warnings),
    )
    for key, error in failures.items():
        logger.error("  FAILED %s: %s", key, error)
    for key, source_warnings in warnings.items():
        for warning in source_warnings:
            logger.warning("  WARN %s: %s", key, warning)
    return 1 if failures else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

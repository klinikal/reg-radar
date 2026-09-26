"""Shared source-health logic: fetching a source exactly as the production
collector does, checking its parser output against the fixture/live-URL
contract, and filing/updating/closing GitHub issues for failures.

Used by both `scripts/capture_fixture.py` (manual fixture (re)capture) and
the nightly `source-health-nightly` workflow (live contract check), so the
fetch path, parse path and assertion are defined exactly once.
"""
import argparse
import json
import logging
import os
import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from collector.adapters.base import FetchContext
from collector.adapters.registry import get as get_adapter
from collector.detail import fetch_detail_body
from collector.main import load_regulators as _load_regulators_split
from collector.pipeline import process_raw_documents
from collector.schema import Item
from collector.adapters.http import DEFAULT_USER_AGENT, BlockedFetchError

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
    it.

    One deliberate divergence from the collector: a blocked fetch is retried
    through FlareSolverr even when the regulator isn't marked
    cloudflare_protected, because this runs from a datacentre IP the
    collector doesn't (#85/#86). So a source can pass here via a path the
    collector would not take - that is the point, and it is logged."""
    source_type = source.get("type", "rss")
    adapter = get_adapter(source_type)
    if adapter is None:
        raise ValueError(f"unknown source type '{source_type}'")

    used_flaresolverr = regulator.get("cloudflare_protected", False)
    ctx = FetchContext(
        user_agent=cfg.get("user_agent", DEFAULT_USER_AGENT),
        # Per-source timeout/retries override, same as collector.fetcher -
        # a source known to be reliably slow gets a longer fuse here too,
        # not just in production collection.
        timeout=int(source.get("timeout", cfg.get("timeout", 30))),
        retries=int(source.get("retries", cfg.get("retries", 3))),
        flaresolverr_url=cfg.get("flaresolverr_url") if used_flaresolverr else None,
        regulator_id=regulator["id"],
    )
    try:
        content = adapter.fetch_raw(source, ctx)
    except BlockedFetchError as blocked:
        # A source not marked cloudflare_protected can still get
        # Cloudflare-challenged from this runner's IP (issue #85/#86) -
        # retry once through FlareSolverr before giving up, rather than
        # reporting a block that a UA/config fix can't touch.
        if used_flaresolverr or not cfg.get("flaresolverr_url"):
            raise
        ctx = replace(ctx, flaresolverr_url=cfg["flaresolverr_url"])
        try:
            content = adapter.fetch_raw(source, ctx)
        except Exception as retry_error:
            # The block signature is the one thing the operator needs, and a
            # broken FlareSolverr raises its own generic message - report
            # what the source actually did, keeping the retry as the cause.
            raise blocked from retry_error
        # A source that passes only through a browser is materially
        # different from one that passes plainly, and the distinction is
        # invisible in a green run unless it's said out loud.
        logger.warning(
            "%s/%s was blocked on a plain fetch (%s) and only succeeded through FlareSolverr",
            regulator["id"], source.get("label", "<unknown>"), blocked,
        )
    return content.encode("utf-8") if isinstance(content, str) else content


def parse_fixture(source: dict, regulator_id: str, content: bytes, fetch_pdf: bool = True) -> list[Item]:
    """Runs the registered adapter's parse() against raw fixture/live
    content, then the shared pipeline - the same code path the collector
    itself uses (../wiki/content/concept-source-adapter-framework.md).

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
        for i in items if i.published_at is not None and not i.date_inferred
    ]
    return max(dates) if dates else None


def check_staleness(items: list[Item], source: dict, now: datetime | None = None) -> tuple[str | None, str | None]:
    """Returns (error, warning), either or both None. No date to judge from
    means no assertion: silence is better than a false alarm on a source
    whose dates are all inferred.

    Note what this can and cannot assess. It reads the dates a source
    supplies, from one live fetch, so it answers "is this source still
    publishing?" It cannot answer "is this source still being collected?" -
    that needs collection history, and this module deliberately has no
    database (the nightly workflow runs it in CI, where none exists).

    A `page_watch` source therefore cannot be judged here at all: its item
    carries a fetch-time date, not the source's, so its age is always zero.
    `staleness:` on a page_watch source is rejected by registry validation
    rather than accepted and silently ignored."""
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


def check_source(regulator: dict, source: dict, cfg: dict, warnings: list | None = None,
                  items_out: list | None = None) -> str | None:
    """Live equivalent of the offline contract test: fetches the source and
    runs the same assertions the fixture test does, plus the staleness
    assertion. Returns None on success, else a string describing the
    failure.

    A staleness *warning* is not a failure - it would file an issue for
    every genuinely quiet regulator - so it is appended to `warnings`
    (if a list was passed) rather than returned.

    Pass a list as `items_out` to also collect the parsed items on success -
    check_detail_page's caller uses this to get a real article URL to probe
    without a second feed fetch."""
    try:
        content = fetch_raw(regulator, source, cfg)
        items = parse_fixture(source, regulator["id"], content)
        assert_contract(items)
    except Exception as e:
        return str(e)
    if items_out is not None:
        items_out.extend(items)
    error, warning = check_staleness(items, source)
    if warning is not None and warnings is not None:
        warnings.append(warning)
    return error


def check_detail_page(regulator: dict, source: dict, url: str, cfg: dict) -> str | None:
    """Issue #109 follow-up: live probe of the detail-page enrichment fetch
    a regulator with `detail_selector` gets at collection time
    (collector/main.py _fill_detail_body) - fetches `url` (a real item's
    link) through the same collector/detail.py fetch_detail_body() the
    collector itself uses, one request per regulator per nightly run.

    Mirrors fetch_raw's own "retry a block through FlareSolverr even for a
    regulator not marked cloudflare_protected" divergence (issue #85/#86):
    this runs from a datacentre IP the production collector doesn't, so a
    plain block here doesn't necessarily mean the collector would see one
    too. A failure never touches the feed source's own health - it is
    reported under its own '<regulator_id>/detail_page' key so a persistent
    block still gets its own issue without being conflated with the feed's.

    Returns None on success, else fetch_detail_body's human-readable reason.
    """
    used_flaresolverr = regulator.get("cloudflare_protected", False)
    kwargs = dict(
        detail_selector=regulator["detail_selector"],
        detail_exclude=regulator.get("detail_exclude"),
        user_agent=cfg.get("user_agent", DEFAULT_USER_AGENT),
        timeout=int(source.get("timeout", cfg.get("timeout", 30))),
        retries=int(source.get("retries", cfg.get("retries", 3))),
    )
    result = fetch_detail_body(url, flaresolverr_url=cfg.get("flaresolverr_url") if used_flaresolverr else None, **kwargs)
    if result["status"] == "blocked" and not used_flaresolverr and cfg.get("flaresolverr_url"):
        result = fetch_detail_body(url, flaresolverr_url=cfg["flaresolverr_url"], **kwargs)
    return None if result["status"] == "recovered" else result["reason"]


def _detail_probe_candidates(items: list[Item], limit: int = 3) -> list[str]:
    """Up to `limit` item URLs worth probing for the detail-page enrichment
    check (see check_detail_page_sample below), filtered to http(s) so a
    relative or malformed url from a misconfigured source is never handed
    to fetch_detail_body."""
    return [i.url for i in items if i.url and i.url.startswith(("http://", "https://"))][:limit]


def check_detail_page_sample(regulator: dict, source: dict, urls: list[str], cfg: dict) -> str | None:
    """Issue #112 follow-up: probes up to `len(urls)` items (see
    _detail_probe_candidates, default 3) via check_detail_page, stopping at
    the first success. Sampling only the single first feed item was a
    frequent false positive - one PDF-only release with no article body, or
    one page an ad-blocker/edge quirk mangles, failed the whole regulator
    even though every other item's detail page was fine. Only reporting a
    failure when every sampled item fails turns that into real signal.

    Returns None on the first success, else a message listing every URL
    tried and its own failure reason."""
    if not urls:
        return "no item url to probe"
    reasons = []
    for url in urls:
        reason = check_detail_page(regulator, source, url, cfg)
        if reason is None:
            return None
        reasons.append(f"{url}: {reason}")
    return "all " + str(len(reasons)) + " sampled item(s) failed:\n" + "\n".join(reasons)


def issue_title(regulator_id: str, source_label: str) -> str:
    return f"source-health: {regulator_id}/{source_label} failing"


def _gh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=check)


def ensure_issue_label() -> None:
    """Create ISSUE_LABEL if the repo does not have it.

    Without this the whole nightly run died on the first failing source:
    `gh issue create --label source-health` exits 1 when the label does not
    exist, the CalledProcessError was unhandled, and the sweep aborted before
    checking the rest of the registry. It had been doing that every night,
    which meant the alarm for every regulator was not merely noisy but
    entirely mute - and mute in a way that looked like a red workflow rather
    than a missing label.
    """
    result = _gh("label", "create", ISSUE_LABEL, "--description",
                 "Opened automatically by the nightly source-health check",
                 "--color", "d4c5f9", check=False)
    if result.returncode == 0:
        logger.info("created the %r label", ISSUE_LABEL)
    elif "already exists" not in (result.stderr or ""):
        logger.warning("could not ensure the %r label exists: %s",
                       ISSUE_LABEL, (result.stderr or "").strip()[:200])


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


def open_or_update_issue(regulator_id: str, source_label: str, error: str, diff: str = "") -> bool:
    """Returns True if the failure was reported. A reporting failure is
    logged and swallowed: one unreachable GitHub call must not stop the
    sweep from checking the remaining sources, which is exactly what used to
    happen. It is never silent - the caller counts these and main() reports
    them separately from source failures, because "the source is broken" and
    "we could not tell you the source is broken" are different problems."""
    title = issue_title(regulator_id, source_label)
    body = f"Source `{regulator_id}/{source_label}` failed its nightly health check.\n\nError: {error}\n"
    if diff:
        body += f"\nDiff against last-known-good fixture:\n```\n{diff}\n```\n"
    try:
        number = find_open_issue(regulator_id, source_label)
        if number:
            _gh("issue", "comment", number, "--body", body)
        else:
            _gh("issue", "create", "--title", title, "--label", ISSUE_LABEL, "--body", body)
        return True
    except subprocess.CalledProcessError as e:
        logger.error("could not report %s/%s: gh exited %s: %s",
                     regulator_id, source_label, e.returncode, (e.stderr or "").strip()[:200])
        return False


def close_issue_if_open(regulator_id: str, source_label: str) -> bool:
    try:
        number = find_open_issue(regulator_id, source_label)
        if number:
            _gh("issue", "close", number, "--comment", "Source recovered on the latest nightly check - closing automatically.")
        return True
    except subprocess.CalledProcessError as e:
        logger.error("could not close the issue for %s/%s: gh exited %s: %s",
                     regulator_id, source_label, e.returncode, (e.stderr or "").strip()[:200])
        return False


def run_all_checks(regulators: list[dict], cfg: dict, file_issues: bool = True,
                    warnings: dict | None = None,
                    report_failures: list | None = None) -> dict[str, str | None]:
    """Runs check_source() for every enabled source and, when file_issues is
    True, opens/updates an issue per failure and closes any that recovered.
    Returns {"regulator_id/source_label": error_or_None}.

    Pass a dict as `warnings` to also collect non-failing staleness
    warnings, keyed the same way."""
    results: dict[str, str | None] = {}
    detail_checked: set[str] = set()
    if file_issues:
        ensure_issue_label()
    for regulator, source in iter_enabled_sources(regulators):
        key = f"{regulator['id']}/{source['label']}"
        source_warnings: list[str] = []
        # #109 follow-up: probe the detail-page enrichment fetch too, once
        # per regulator, off whichever rss source's items land first - the
        # collector only ever makes this fetch for an rss source on a
        # regulator with detail_selector (collector/main.py
        # _fill_detail_body), so that is the only shape worth mirroring here.
        wants_detail_check = (
            bool(regulator.get("detail_selector"))
            and source.get("type") == "rss"
            and regulator["id"] not in detail_checked
        )
        items_out: list = [] if wants_detail_check else None
        error = check_source(regulator, source, cfg, warnings=source_warnings, items_out=items_out)
        results[key] = error
        if warnings is not None and source_warnings:
            warnings[key] = source_warnings
        if file_issues:
            reported = (open_or_update_issue(regulator["id"], source["label"], error) if error
                        else close_issue_if_open(regulator["id"], source["label"]))
            if not reported and report_failures is not None:
                report_failures.append(key)

        if wants_detail_check and error is None and items_out:
            detail_checked.add(regulator["id"])
            detail_key = f"{regulator['id']}/detail_page"
            candidate_urls = _detail_probe_candidates(items_out)
            detail_error = check_detail_page_sample(regulator, source, candidate_urls, cfg)
            results[detail_key] = detail_error
            if file_issues:
                reported = (open_or_update_issue(regulator["id"], "detail_page", detail_error) if detail_error
                            else close_issue_if_open(regulator["id"], "detail_page"))
                if not reported and report_failures is not None:
                    report_failures.append(detail_key)
    return results


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check every enabled source's live health and file/update/close GitHub issues for failures.",
    )
    parser.add_argument(
        "--no-file-issues", action="store_true",
        help="Dry run: check sources and print results, but don't open, update or close any GitHub issue.",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from collector.main import load_config

    args = parse_args(argv)

    regulators = load_regulators()
    cfg = load_config()
    warnings: dict[str, list[str]] = {}
    report_failures: list[str] = []
    results = run_all_checks(regulators, cfg, file_issues=not args.no_file_issues,
                              warnings=warnings, report_failures=report_failures)

    failures = {k: v for k, v in results.items() if v}
    logger.info(
        "source health: %d checked, %d failed, %d warned, %d unreported",
        len(results), len(failures), len(warnings), len(report_failures),
    )
    for key, error in failures.items():
        logger.error("  FAILED %s: %s", key, error)
    for key, source_warnings in warnings.items():
        for warning in source_warnings:
            logger.warning("  WARN %s: %s", key, warning)
    if report_failures:
        # Distinct from a failing source: the check ran, but nobody was told.
        # Worth its own line, because a silent reporting path is how this
        # whole job sat broken for as long as it did.
        logger.error(
            "  %d source(s) could not be reported to GitHub: %s",
            len(report_failures), ", ".join(report_failures),
        )
    # Every checked source failing at once is not 44 regulators breaking in
    # the same run - it's this job losing egress (DNS, proxy, firewall,
    # runner network outage). Reporting that as ordinary source failures
    # would bury the real cause under a wall of individually-unremarkable
    # issues, so it gets its own loud failure instead.
    if len(results) > 1 and len(failures) == len(results):
        logger.error(
            "all %d checked sources failed - this looks like an egress/network problem for this "
            "job, not %d broken regulators. Check the runner's network access before chasing "
            "individual source issues.",
            len(results), len(results),
        )
        return 1

    # A failing source is already handled: it gets its own GitHub issue
    # (opened, commented on if it recurs, auto-closed on recovery), which is
    # its own notification. Failing the workflow *too* means a red run and
    # an email for every flaky regulator site on top of that - noise, not
    # signal, for a source that self-recovers most nights (CNMV: issues
    # #83/#84/#89/#90). Only fail the job - and page - when the automated
    # tracking itself broke and a human is the only backstop left.
    return 1 if report_failures else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

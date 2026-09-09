"""(Re)captures a regression fixture for tests/test_source_fixtures.py by
fetching a source through the exact production fetch path (same
_fetch_html/_fetch_bytes, same FlareSolverr wiring and throttling
fetch_source() uses) and writing the raw response unmodified.

    python -m scripts.capture_fixture --regulator <id> --source <label> [--dry-run]
    python -m scripts.capture_fixture --all [--dry-run]

`--all` iterates every enabled source, reusing fetch_source()'s existing
per-domain FlareSolverr throttle rather than adding a second one. This stays
a manual local tool - it is never invoked by CI or the nightly workflow, and
last-known-good fixtures only change on a deliberate run of this script.
"""
import argparse
import logging
import sys

from collector.main import load_config
from scripts.source_health import FIXTURE_WARN_BYTES, capture, iter_enabled_sources, load_regulators

logger = logging.getLogger(__name__)


def main(argv=None) -> int:
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s %(message)s")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--regulator", help="regulator id (with --source)")
    parser.add_argument("--source", help="source label (with --regulator)")
    parser.add_argument("--all", action="store_true", help="capture every enabled source")
    parser.add_argument("--dry-run", action="store_true", help="fetch and validate but do not write fixture files")
    parser.add_argument("--config", default=None, help="path to the regulators config file or directory (default: config/regulators)")
    args = parser.parse_args(argv)

    if not args.all and not (args.regulator and args.source):
        parser.error("either --all, or both --regulator and --source, are required")

    regulators = load_regulators(args.config)
    cfg = load_config()

    if args.all:
        targets = list(iter_enabled_sources(regulators))
    else:
        targets = [
            (reg, source)
            for reg, source in iter_enabled_sources(regulators)
            if reg["id"] == args.regulator and source["label"] == args.source
        ]
        if not targets:
            parser.error(f"no enabled source {args.regulator}/{args.source} found in {args.config or 'config/regulators'}")

    captured = 0
    failed: list[tuple[str, str, str]] = []
    for reg, source in targets:
        try:
            path, size = capture(reg, source, cfg, dry_run=args.dry_run)
            warn = f" (WARNING: {size} bytes exceeds the ~{FIXTURE_WARN_BYTES // 1024}KB sanity threshold)" if size > FIXTURE_WARN_BYTES else ""
            suffix = " [dry-run, not written]" if args.dry_run else ""
            logger.info("captured %s/%s -> %s (%d bytes)%s%s", reg["id"], source["label"], path, size, warn, suffix)
            captured += 1
        except Exception as e:
            logger.warning("FAILED to capture %s/%s: %s", reg["id"], source["label"], e)
            failed.append((reg["id"], source["label"], str(e)))

    logger.info("done: %d captured, %d failed", captured, len(failed))
    for regulator_id, label, error in failed:
        logger.info("  FAILED %s/%s: %s", regulator_id, label, error)

    return 1 if failed and not args.all else 0


if __name__ == "__main__":
    sys.exit(main())

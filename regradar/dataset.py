"""Published dataset packaging and import (architecture doc section 8, row
10 / issue #12): the manifest + tarball builder `.github/workflows/
publish-dataset.yml` drives, and the `regradar import-dataset <url|path>`
command that loads a published dataset into an empty database - which
saves a new install the historic backfill scripts, not the routine
collection run (that fetches current feed pages either way).

Wired into regradar/cli.py's subcommand registry (add_subparser()/run() -
see that module's docstring for the convention).

Manifest shape (issue #10's "Manifest shape"):

    {
      "regradar_schema": 1,
      "taxonomy_version": 3,
      "generated_at": "2026-09-09T02:14:00Z",
      "item_count": 41823,
      "per_regulator_counts": {"esma": 7260, "fca": 9743},
      "excluded_regulators": ["mfsa"],
      "sha256": "...",
      "tarball": "reg-radar-dataset-2026-09-09.tar.gz"
    }

The manifest is written BESIDE the tarball, never inside it - its own
`sha256` field describes that exact tarball, so embedding manifest.json
inside the archive being hashed would make the file describe its own
pre-manifest state, not the one actually shipped. `find_manifest()` on the
import side still also checks inside the tarball for a `manifest.json`
member, for a tarball assembled some other way (e.g. by hand for local
testing) that already embeds one referring to itself.
"""
import argparse
import hashlib
import json
import logging
import os
import sys
import tarfile
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml
from sqlalchemy import text

from collector.pipeline import truncate_snippet
from collector.schema import Item
from collector.storage import init_db, insert_item, set_sync_state, upsert_regulator
from regradar.changes import DATASET_IMPORT_CURSOR_KEY, encode_cursor
from regradar.classification import import_classification, materialize_labels
from regradar.taxonomy import load_and_exit_on_error

logger = logging.getLogger(__name__)

MANIFEST_FILENAME = "manifest.json"
JSONL_FILENAME = "export.jsonl"
SYNC_STATE_KEY = DATASET_IMPORT_CURSOR_KEY
DEFAULT_TIMEOUT_SECONDS = 60
# GitHub's per-release-asset limit, so a download larger than this cannot be
# the dataset we published and is either the wrong URL or something hostile.
DEFAULT_MAX_BYTES = 2 * 1024 ** 3
# The tie-break id half of the stored cursor (regradar.changes'
# "<updated_at>|<item_id>" shape - encode_cursor() requires a non-empty
# id). Real item ids are lowercase-hex sha256 digests (0-9a-f), so any
# string starting with a character above 'f' sorts after every real id;
# that makes get_changes()'s equal-timestamp tie-break ("id > since_id")
# false for every real item, i.e. `generated_at` behaves as an exclusive
# cutoff with no real id needed to make it one.
_CURSOR_SENTINEL_ID = "z"


class DatasetError(ValueError):
    """Raised for a malformed/tampered dataset or a refused import."""


def load_exclusions(path: str) -> set[str]:
    """Flat YAML list of regulator ids (config/dataset_exclusions.yaml) to
    a set. A missing or empty file means "no exclusions" - the file is
    optional, same convention as config/webhooks.yaml.
    """
    p = Path(path)
    if not p.exists():
        return set()
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not data:
        return set()
    return set(data)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def create_tarball(export_dir: Path, tarball_path: Path) -> None:
    """tar.gz of export_dir's own contents (the section 3.1 markdown tree
    plus export.jsonl, and originals/ if --with-originals was used),
    rooted so extracting the tarball reproduces export_dir's layout
    directly with no wrapper directory.
    """
    with tarfile.open(tarball_path, "w:gz") as tar:
        for child in sorted(export_dir.iterdir()):
            if child.name == MANIFEST_FILENAME:
                continue
            tar.add(child, arcname=child.name)


def build_manifest(jsonl_path: Path, *, taxonomy_version: int, excluded_regulators: set,
                    tarball_name: str, sha256: str, generated_at: datetime | None = None) -> dict:
    """Counts item_count/per_regulator_counts directly from export.jsonl -
    the same file that goes into the tarball - so the manifest can never
    drift from what's actually published (issue #10 acceptance criterion:
    "item_count and per_regulator_counts match the exported corpus").
    """
    per_regulator: dict = {}
    item_count = 0
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        doc = json.loads(line)
        item_count += 1
        reg = doc.get("regulator", "unknown")
        per_regulator[reg] = per_regulator.get(reg, 0) + 1
    return {
        "regradar_schema": 1,
        "taxonomy_version": taxonomy_version,
        "generated_at": (generated_at or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "item_count": item_count,
        "per_regulator_counts": per_regulator,
        "excluded_regulators": sorted(excluded_regulators),
        "sha256": sha256,
        "tarball": tarball_name,
    }


def publish_dataset(export_dir: Path, dist_dir: Path, *, tarball_name: str, taxonomy_version: int,
                     excluded_regulators: set, generated_at: datetime | None = None) -> dict:
    """One-shot packaging step: tars export_dir, hashes the tarball, builds
    manifest.json from export_dir/export.jsonl, and writes both to
    dist_dir. Returns the manifest dict. This is what `.github/workflows/
    publish-dataset.yml` calls after `regradar export` has already
    written export_dir.
    """
    dist_dir.mkdir(parents=True, exist_ok=True)
    tarball_path = dist_dir / tarball_name
    create_tarball(export_dir, tarball_path)
    manifest = build_manifest(
        export_dir / JSONL_FILENAME,
        taxonomy_version=taxonomy_version,
        excluded_regulators=excluded_regulators,
        tarball_name=tarball_name,
        sha256=sha256_file(tarball_path),
        generated_at=generated_at,
    )
    (dist_dir / MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2, sort_keys=False), encoding="utf-8")
    return manifest


def _download(url: str, dest_dir: Path, *, timeout: float = DEFAULT_TIMEOUT_SECONDS,
               max_bytes: int = DEFAULT_MAX_BYTES) -> Path:
    """Streams `url` to a file under dest_dir. `timeout` bounds the connect
    and each socket read, so a hung or trickling server can't wedge the
    command forever; `max_bytes` caps the total written, so an endless or
    mis-addressed response can't fill the disk. Either failure deletes the
    partial file before raising - a truncated tarball must never be left
    behind for a later run to find and trust.
    """
    dest = dest_dir / "dataset.tar.gz"
    logger.info("Downloading dataset from %s", url)
    written = 0
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp, dest.open("wb") as f:  # noqa: S310 - operator-supplied URL, CLI tool
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise DatasetError(
                        f"download from {url} exceeded the {max_bytes} byte limit "
                        "- wrong URL, or raise --max-bytes if the dataset really is this large"
                    )
                f.write(chunk)
    except BaseException as e:
        dest.unlink(missing_ok=True)
        if isinstance(e, (DatasetError, KeyboardInterrupt, SystemExit)):
            raise
        raise DatasetError(f"download from {url} failed: {e}") from e
    return dest


def _find_manifest(tarball_path: Path) -> dict:
    sibling = tarball_path.parent / MANIFEST_FILENAME
    if sibling.exists():
        return json.loads(sibling.read_text(encoding="utf-8"))
    with tarfile.open(tarball_path, "r:gz") as tar:
        try:
            member = tar.getmember(MANIFEST_FILENAME)
        except KeyError:
            raise DatasetError(
                f"no manifest.json found beside {tarball_path} or inside the tarball"
            ) from None
        f = tar.extractfile(member)
        if f is None:
            raise DatasetError(f"manifest.json inside {tarball_path} is not a regular file")
        return json.loads(f.read().decode("utf-8"))


def _extract(tarball_path: Path, dest_dir: Path) -> None:
    with tarfile.open(tarball_path, "r:gz") as tar:
        # filter="data" is load-bearing, not decoration: on Python 3.11 the
        # default filter is still the legacy one that happily writes absolute
        # paths and ../ traversal outside dest_dir. The sha256 check above
        # only proves the archive matches its manifest, which says nothing
        # about what the manifest's author put in it.
        tar.extractall(dest_dir, filter="data")


def import_dataset(engine, source: str, *, taxonomy, force: bool = False,
                    work_dir: Path | None = None, timeout: float = DEFAULT_TIMEOUT_SECONDS,
                    max_bytes: int = DEFAULT_MAX_BYTES) -> dict:
    """Downloads or reads `source` (a URL or a local path to either the
    tarball itself or a directory already containing one), verifies its
    sha256 against the manifest, refuses a non-empty items table unless
    `force` is set, then loads every item and its classifications from
    export.jsonl, and records the manifest's `generated_at` as a sync
    cursor. Returns a summary dict: {"items_loaded", "item_count",
    "excluded_regulators", "generated_at", "cursor"}.

    The cursor marks where the snapshot ends. It does NOT make later
    collection incremental - a collection run fetches each source's current
    feed pages regardless - and nothing applies it automatically. What
    importing actually saves is the historic backfill scripts (README's
    "Historic backfills"), which are the slow part. The cursor is a
    bookmark for a change-stream consumer that wants only post-import
    changes: pass it as `since` to get_changes()/`/api/changes`/a webhook
    subscriber, or to `regradar export --since` for a one-off. It is
    surfaced by get_health()'s `dataset_import`.
    """
    with tempfile.TemporaryDirectory(dir=work_dir) as tmp:
        tmp_path = Path(tmp)
        if source.startswith("http://") or source.startswith("https://"):
            tarball_path = _download(source, tmp_path, timeout=timeout, max_bytes=max_bytes)
        else:
            tarball_path = Path(source)
            if not tarball_path.exists():
                raise DatasetError(f"no such file: {tarball_path}")

        manifest = _find_manifest(tarball_path)
        actual_sha256 = sha256_file(tarball_path)
        if actual_sha256 != manifest.get("sha256"):
            raise DatasetError(
                f"sha256 mismatch: tarball is {actual_sha256}, manifest says {manifest.get('sha256')} "
                "- refusing to import a tampered or corrupted dataset"
            )

        with engine.connect() as conn:
            existing = conn.execute(text("SELECT COUNT(*) FROM items")).scalar()
        if existing and not force:
            raise DatasetError(
                f"items table already has {existing} row(s) - pass --force to import anyway"
            )

        extract_dir = tmp_path / "extracted"
        extract_dir.mkdir()
        _extract(tarball_path, extract_dir)
        jsonl_path = extract_dir / JSONL_FILENAME
        if not jsonl_path.exists():
            raise DatasetError(f"{JSONL_FILENAME} not found in the dataset")

        seen_regulators: set[str] = set()
        updated_ats: list[dict] = []
        items_loaded = 0
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            doc = json.loads(line)
            item_id = doc["id"]
            regulator_id = doc["regulator"]
            # export.jsonl carries the item's body directly (the exporter
            # runs get_changes(include_body=True), which applies the
            # raw_content fallback for pre-v2 rows), so the markdown tree
            # in the tarball is for humans, not a second parse target.
            body_md = doc.get("body_md") or ""

            if regulator_id not in seen_regulators:
                upsert_regulator(engine, {
                    "id": regulator_id,
                    "name": doc.get("regulator_name") or regulator_id,
                    "full_name": doc.get("regulator_name") or regulator_id,
                    "jurisdiction": doc.get("jurisdiction"),
                    "enabled": True,
                })
                seen_regulators.add(regulator_id)

            item = Item(
                id=item_id,
                regulator_id=regulator_id,
                source_label=doc.get("source_label") or "",
                title=doc.get("title") or "",
                url=doc.get("source_url") or "",
                published_at=_parse_dt(doc.get("published_at")),
                fetched_at=_parse_dt(doc.get("fetched_at")) or datetime.now(timezone.utc),
                date_inferred=bool(doc.get("date_inferred")),
                language=doc.get("language") or "",
                content_snippet=truncate_snippet(body_md),
                source_type=doc.get("source_type") or "",
                # raw_content mirrors body_md: items_fts indexes title/
                # content_snippet/raw_content, never body_md, so an imported
                # corpus would otherwise be title-only searchable.
                raw_content=body_md,
                body_md=body_md,
                extractor="import",
            )
            inserted = insert_item(engine, item)
            if not inserted:
                continue
            items_loaded += 1

            for row in doc.get("label_provenance") or []:
                import_classification(
                    engine, item_id, row["axis"], row["value"],
                    taxonomy_version=row.get("taxonomy_version") or manifest.get("taxonomy_version") or 1,
                    confidence=row.get("confidence") if row.get("confidence") is not None else 0.5,
                    by=row.get("by") or "import",
                )
            if doc.get("label_provenance"):
                materialize_labels(engine, taxonomy, item_id)

            updated_at = _parse_dt(doc.get("updated_at")) or item.fetched_at
            # Naive UTC: a raw-SQL datetime write stores its repr verbatim,
            # and get_changes() string-compares updated_at against the naive
            # format the ORM writes (see collector.storage.db_now).
            updated_ats.append({"id": item_id, "u": updated_at.replace(tzinfo=None)})

        if updated_ats:
            with engine.connect() as conn:
                conn.execute(text("UPDATE items SET updated_at = :u WHERE id = :id"), updated_ats)
                conn.commit()

        generated_at = manifest.get("generated_at")
        cursor = encode_cursor(generated_at, _CURSOR_SENTINEL_ID) if generated_at else None
        if cursor:
            set_sync_state(engine, SYNC_STATE_KEY, cursor)

        return {
            "items_loaded": items_loaded,
            "item_count": manifest.get("item_count"),
            "excluded_regulators": manifest.get("excluded_regulators") or [],
            "generated_at": generated_at,
            "cursor": cursor,
        }


def _parse_dt(value):
    if not value:
        return None
    if isinstance(value, str):
        raw = value.replace("T", " ")
        if raw.endswith("Z"):
            raw = raw[:-1]
        return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
    return value


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "import-dataset", help="Load a published dataset (tarball) into an empty database",
    )
    parser.add_argument("source", help="URL or local path to the dataset tarball")
    parser.add_argument("--force", action="store_true", help="Import even if the database already has items")
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Per-read timeout in seconds when source is a URL (default: {DEFAULT_TIMEOUT_SECONDS})",
    )
    parser.add_argument(
        "--max-bytes", type=int, default=DEFAULT_MAX_BYTES,
        help=f"Abort a URL download larger than this (default: {DEFAULT_MAX_BYTES}, GitHub's asset limit)",
    )
    parser.add_argument("--db-path", default=os.getenv("DB_PATH", "/app/data/reg_radar.db"))
    parser.add_argument("--taxonomy-path", default=os.getenv("TAXONOMY_PATH", "config/taxonomy.yaml"))
    parser.add_argument("--jurisdictions-path", default=os.getenv("JURISDICTIONS_PATH", "config/jurisdictions.yaml"))
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    # init_db (not a bare create_engine like `export`/`render` use): the
    # whole point of this command is seeding a brand-new install, whose
    # database file may not exist yet, let alone have a schema. It's
    # idempotent, so pointing it at a live database is harmless.
    engine = init_db(args.db_path)
    taxonomy = load_and_exit_on_error(args.taxonomy_path, args.jurisdictions_path)
    try:
        summary = import_dataset(
            engine, args.source, taxonomy=taxonomy, force=args.force,
            timeout=args.timeout, max_bytes=args.max_bytes,
        )
    except DatasetError as e:
        print(f"regradar import-dataset: {e}", file=sys.stderr)
        return 1

    print(f"Imported {summary['items_loaded']} item(s) (dataset generated at {summary['generated_at']})")
    excluded = summary["excluded_regulators"]
    if excluded:
        print(
            f"{len(excluded)} regulator(s) excluded from this dataset, need a manual backfill if wanted: "
            + ", ".join(excluded)
        )
    if summary["cursor"]:
        # Nothing applies this automatically - a collection run fetches the
        # current feed pages either way. Print it so "what arrived since the
        # snapshot" is a copy-paste away rather than a cursor nobody reads.
        print(f"\nSync cursor stored: {summary['cursor']}")
        print("Everything new since this snapshot:")
        print(f"  reg-radar export --format jsonl --out <dir> --since {summary['cursor']}")
        print(f"  get_changes_tool(since=\"{summary['cursor']}\")")
        print("It is also reported as get_health_tool()'s `dataset_import`.")
    return 0

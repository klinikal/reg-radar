"""Re-runs Markdown body conversion from stored artefacts for items whose
`extractor` is older than the currently installed conversion library - e.g.
after a markdownify or pypdf version bump. Items with no stored artefact
(RSS entries, or scrape items collected before STORE_ARTEFACTS existed) are
left untouched; nothing to reprocess them from.

Run inside the collector container, which has both DB and artefact access:

    docker compose exec collector python -m scripts.reprocess_bodies --dry-run
    docker compose exec collector python -m scripts.reprocess_bodies
    docker compose exec collector python -m scripts.reprocess_bodies --limit 50
"""
import argparse
import importlib.metadata
import logging
import os
from pathlib import Path

from sqlalchemy import text

from collector.adapters.http import extract_pdf_text as _extract_pdf_text
from collector.storage import init_db, update_item_body
from regradar.document import convert_body

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

_DEFAULT_ARTEFACTS_DIR = "/app/data/originals"


def _library_version(package: str) -> str:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _current_extractor(mime: str) -> str:
    package = "pypdf" if mime == "application/pdf" else "markdownify"
    return f"{package}/{_library_version(package)}"


def _reconvert(content: bytes, mime: str) -> tuple[str, str]:
    if mime == "application/pdf":
        return convert_body(pdf_text=_extract_pdf_text(content) or "")
    return convert_body(content_html=content.decode("utf-8", errors="replace"))


def reprocess(engine, artefacts_dir: str, dry_run: bool, limit: int | None) -> int:
    sql = """
        SELECT i.id, i.extractor, a.path, a.mime
        FROM items i JOIN item_artefacts a ON a.item_id = i.id
        ORDER BY i.id
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql)).fetchall()

    stale = [r for r in rows if r[1] != _current_extractor(r[3])]
    if limit is not None:
        stale = stale[:limit]

    if dry_run:
        logger.info("Dry run: %d item(s) would be reprocessed (of %d with an artefact)", len(stale), len(rows))
        return len(stale)

    processed = 0
    for item_id, _old_extractor, rel_path, mime in stale:
        full_path = Path(artefacts_dir) / rel_path
        if not full_path.exists():
            logger.warning("Artefact missing on disk for %s: %s", item_id, full_path)
            continue
        body_md, extractor = _reconvert(full_path.read_bytes(), mime)
        update_item_body(engine, item_id, body_md, extractor)
        processed += 1
    logger.info("Reprocessed %d item(s)", processed)
    return processed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Report the count without writing")
    parser.add_argument("--limit", type=int, default=None, help="Reprocess at most this many items")
    args = parser.parse_args()

    db_path = os.getenv("DB_PATH", "/app/data/regradar.db")
    artefacts_dir = os.getenv("ARTEFACTS_DIR", _DEFAULT_ARTEFACTS_DIR)
    engine = init_db(db_path)
    reprocess(engine, artefacts_dir, dry_run=args.dry_run, limit=args.limit)


if __name__ == "__main__":
    main()

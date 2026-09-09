import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    create_engine,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Session

from collector.schema import Item

logger = logging.getLogger(__name__)

# Original-artefact storage (docs/design/regradar-architecture.md section
# 3.3). Read at call time (not import time) so tests can point a single
# call at a temp directory without needing to reload this module.
_DEFAULT_ARTEFACTS_DIR = "/app/data/originals"
_DEFAULT_MAX_ARTEFACT_BYTES = 25 * 1024 * 1024  # matches MAX_PDF_DOWNLOAD_BYTES's existing default


def _artefacts_dir() -> str:
    return os.getenv("ARTEFACTS_DIR", _DEFAULT_ARTEFACTS_DIR)


def _max_artefact_bytes() -> int:
    return int(os.getenv("MAX_ARTEFACT_BYTES", str(_DEFAULT_MAX_ARTEFACT_BYTES)))


def _store_artefacts_enabled() -> bool:
    return os.getenv("STORE_ARTEFACTS", "true").strip().lower() not in ("false", "0", "")


class Base(DeclarativeBase):
    pass


class RegulatorRow(Base):
    __tablename__ = "regulators"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    full_name = Column(String)
    jurisdiction = Column(String)
    jurisdiction_type = Column(String)
    website = Column(String)
    mica_competent_authority = Column(Boolean, default=False)
    enabled = Column(Boolean, default=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class ItemRow(Base):
    __tablename__ = "items"

    id = Column(String, primary_key=True)
    regulator_id = Column(String, nullable=False)
    source_label = Column(String)
    title = Column(String)
    url = Column(String)
    published_at = Column(DateTime, nullable=True)
    fetched_at = Column(DateTime)
    date_inferred = Column(Boolean, default=False)
    language = Column(String)
    content_snippet = Column(Text)
    source_type = Column(String)
    source_tags = Column(Text)  # JSON array - structural (warnings, press_releases, ...)
    content_tags = Column(Text)  # JSON array - what the item is about (keyword-derived today)
    raw_content = Column(Text)
    body_md = Column(Text)
    extractor = Column(String)  # e.g. "markdownify/1.2.3" - which body_md conversion produced this row
    updated_at = Column(DateTime)  # bumps on any change to body, labels or artefact; drives the sync cursor


class RunRow(Base):
    __tablename__ = "runs"

    run_id = Column(String, primary_key=True)
    started_at = Column(DateTime)
    completed_at = Column(DateTime, nullable=True)
    status = Column(String)  # "running" | "success" | "partial" | "failed"
    total_fetched = Column(Integer, default=0)
    total_new = Column(Integer, default=0)
    total_errors = Column(Integer, default=0)
    error_detail = Column(Text)  # JSON array
    db_integrity_ok = Column(Boolean, nullable=True)  # PRAGMA integrity_check result, checked once per run


class SourceErrorRow(Base):
    __tablename__ = "source_errors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    regulator_id = Column(String, nullable=False)
    source_label = Column(String, nullable=False)
    consecutive_failures = Column(Integer, default=0)
    last_error = Column(Text)
    last_attempt_at = Column(DateTime)


class ItemArtefactRow(Base):
    __tablename__ = "item_artefacts"

    item_id = Column(String, primary_key=True)
    path = Column(String, nullable=False)  # relative to ARTEFACTS_DIR
    mime = Column(String, nullable=False)
    sha256 = Column(String, nullable=False)
    bytes = Column(Integer, nullable=False)
    fetched_at = Column(DateTime, nullable=False)


class ItemChunkRow(Base):
    __tablename__ = "item_chunks"

    id = Column(String, primary_key=True)
    item_id = Column(String, nullable=False)
    chunk_index = Column(Integer, nullable=False)
    text = Column(Text, nullable=False)
    token_count = Column(Integer)
    content_hash = Column(String, nullable=False)
    created_at = Column(DateTime)


def _ensure_column(conn, table: str, column: str, ddl_type: str) -> None:
    cols = [row[1] for row in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()]
    if cols and column not in cols:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))
        conn.commit()


def init_db(db_path: str):
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"timeout": 30})
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.execute(text("PRAGMA synchronous=NORMAL"))
        conn.execute(text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5("
            "item_id UNINDEXED, title, content_snippet, raw_content, "
            "tokenize='unicode61')"
        ))
        conn.commit()
        _ensure_column(conn, "items", "source_tags", "TEXT")
        _ensure_column(conn, "items", "content_tags", "TEXT")
        _ensure_column(conn, "items", "body_md", "TEXT")
        _ensure_column(conn, "items", "extractor", "TEXT")
        _ensure_column(conn, "items", "updated_at", "DATETIME")
        _ensure_column(conn, "runs", "db_integrity_ok", "BOOLEAN")
    Base.metadata.create_all(engine)
    return engine


def check_db_integrity(engine) -> bool:
    """Runs SQLite's own PRAGMA integrity_check and returns True only if it
    reports completely clean (a single "ok" row) - any other output, or the
    check itself raising (e.g. "database disk image is malformed" on a
    sufficiently corrupted file), counts as failed. Cheap enough to run every
    collection cycle at this database's size (well under a second).
    """
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("PRAGMA integrity_check")).fetchall()
    except Exception:
        return False
    return len(rows) == 1 and rows[0][0] == "ok"


def upsert_regulator(engine, entry: dict) -> None:
    with Session(engine) as session:
        existing = session.get(RegulatorRow, entry["id"])
        if existing:
            for k, v in entry.items():
                if hasattr(existing, k):
                    setattr(existing, k, v)
            existing.updated_at = datetime.now(timezone.utc)
        else:
            session.add(RegulatorRow(**{k: v for k, v in entry.items() if hasattr(RegulatorRow, k)}))
        session.commit()


def _write_artefact(item: Item) -> dict | None:
    """Writes item.raw_bytes to ARTEFACTS_DIR/<id2>/<id>.<ext> and returns
    the row fields for item_artefacts, or None if there's nothing to store
    (no raw_bytes, STORE_ARTEFACTS=false, or over MAX_ARTEFACT_BYTES).
    """
    if not item.raw_bytes or not _store_artefacts_enabled():
        return None
    if len(item.raw_bytes) > _max_artefact_bytes():
        return None
    ext = "pdf" if item.mime == "application/pdf" else "html"
    id2 = item.id[:2]
    rel_path = f"{id2}/{item.id}.{ext}"
    full_path = Path(_artefacts_dir()) / rel_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_bytes(item.raw_bytes)
    return {
        "item_id": item.id,
        "path": rel_path,
        "mime": item.mime or "application/octet-stream",
        "sha256": hashlib.sha256(item.raw_bytes).hexdigest(),
        "bytes": len(item.raw_bytes),
        "fetched_at": item.fetched_at,
    }


def insert_item(engine, item: Item) -> bool:
    with Session(engine) as session:
        existing = session.get(ItemRow, item.id)
        if existing:
            return False
        row = ItemRow(
            id=item.id,
            regulator_id=item.regulator_id,
            source_label=item.source_label,
            title=item.title,
            url=item.url,
            published_at=item.published_at,
            fetched_at=item.fetched_at,
            date_inferred=item.date_inferred,
            language=item.language,
            content_snippet=item.content_snippet,
            source_type=item.source_type,
            source_tags=json.dumps(item.source_tags),
            content_tags=json.dumps(item.content_tags),
            raw_content=item.raw_content,
            body_md=item.body_md,
            extractor=item.extractor,
            updated_at=item.fetched_at,
        )
        session.add(row)
        session.execute(
            text(
                "INSERT INTO items_fts (item_id, title, content_snippet, raw_content) "
                "VALUES (:item_id, :title, :content_snippet, :raw_content)"
            ),
            {
                "item_id": item.id,
                "title": item.title or "",
                "content_snippet": item.content_snippet or "",
                "raw_content": item.raw_content or "",
            },
        )
        try:
            artefact = _write_artefact(item)
        except OSError as e:
            logger.warning("Artefact write failed for item %s, storing item without one: %s", item.id, e)
            artefact = None
        if artefact:
            session.execute(
                text(
                    "INSERT INTO item_artefacts (item_id, path, mime, sha256, bytes, fetched_at) "
                    "VALUES (:item_id, :path, :mime, :sha256, :bytes, :fetched_at)"
                ),
                artefact,
            )
        session.commit()
        return True


def get_artefact(engine, item_id: str) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT item_id, path, mime, sha256, bytes, fetched_at FROM item_artefacts WHERE item_id=:id"),
            {"id": item_id},
        ).fetchone()
    if row is None:
        return None
    return dict(row._mapping)


def update_item_body(engine, item_id: str, body_md: str, extractor: str) -> None:
    """Used by scripts/reprocess_bodies.py to re-run Markdown conversion on
    an existing row. Bumps updated_at, per section 3.2's sync-cursor rule.
    """
    with Session(engine) as session:
        row = session.get(ItemRow, item_id)
        if row is None:
            return
        row.body_md = body_md
        row.extractor = extractor
        row.updated_at = datetime.now(timezone.utc)
        session.commit()


def record_run_start(engine) -> str:
    run_id = str(uuid.uuid4())
    with Session(engine) as session:
        session.add(RunRow(
            run_id=run_id,
            started_at=datetime.now(timezone.utc),
            status="running",
        ))
        session.commit()
    return run_id


def record_run_complete(engine, run_id: str, total_fetched: int, total_new: int, total_errors: int, error_detail: list, status: str | None = None, db_integrity_ok: bool | None = None) -> None:
    if status is None:
        status = "success" if total_errors == 0 else "partial"
    with Session(engine) as session:
        row = session.get(RunRow, run_id)
        if row:
            row.completed_at = datetime.now(timezone.utc)
            row.status = status
            row.total_fetched = total_fetched
            row.total_new = total_new
            row.total_errors = total_errors
            row.error_detail = json.dumps(error_detail)
            row.db_integrity_ok = db_integrity_ok
            session.commit()


def record_source_error(engine, regulator_id: str, source_label: str, error: str) -> None:
    with Session(engine) as session:
        row = session.execute(
            text("SELECT id, consecutive_failures FROM source_errors WHERE regulator_id=:r AND source_label=:s"),
            {"r": regulator_id, "s": source_label}
        ).fetchone()
        if row:
            session.execute(
                text(
                    "UPDATE source_errors SET consecutive_failures=:f, last_error=:e, last_attempt_at=:t "
                    "WHERE regulator_id=:r AND source_label=:s"
                ),
                {
                    "f": row[1] + 1,
                    "e": error,
                    "t": datetime.now(timezone.utc),
                    "r": regulator_id,
                    "s": source_label,
                }
            )
        else:
            session.add(SourceErrorRow(
                regulator_id=regulator_id,
                source_label=source_label,
                consecutive_failures=1,
                last_error=error,
                last_attempt_at=datetime.now(timezone.utc),
            ))
        session.commit()


def reset_source_errors(engine, regulator_id: str, source_label: str) -> None:
    with Session(engine) as session:
        session.execute(
            text(
                "UPDATE source_errors SET consecutive_failures=0 "
                "WHERE regulator_id=:r AND source_label=:s"
            ),
            {"r": regulator_id, "s": source_label}
        )
        session.commit()

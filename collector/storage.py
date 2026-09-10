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
    Float,
    Index,
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


def db_now() -> datetime:
    """UTC timestamp for a raw-SQL datetime write (created_at/updated_at/
    superseded_at/last_attempt_at/... via text()). SQLAlchemy's DateTime
    type strips tzinfo before storing on sqlite, so every ORM write
    (`Column(DateTime, default=lambda: datetime.now(timezone.utc))`, or a
    plain `row.updated_at = datetime.now(timezone.utc)`) ends up naive in
    the DB. A raw `text()` INSERT/UPDATE bypasses that stripping and stores
    the tz-aware repr (`...+00:00`) verbatim, which sorts and compares
    differently as a string than the naive ORM format - this is what broke
    the get_changes() cursor for classification writes. Use this instead of
    `datetime.now(timezone.utc)` at every raw-SQL datetime param."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


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
    labels = Column(Text)  # JSON object - materialised effective_labels(), see regradar/classification.py
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
    # A successful fetch that yields zero items despite the site returning
    # 200 OK - the "selector drift" case a redesign causes: the page still
    # loads fine, it just no longer matches anything. consecutive_failures
    # doesn't catch this (no exception is raised), so it needs its own
    # counter, reset only when a fetch actually yields >=1 item.
    consecutive_empty_runs = Column(Integer, default=0)
    # A fetch classified as blocked/challenged rather than served (issue
    # #39): a Cloudflare or Sucuri block, a challenge interstitial carrying
    # a 200, or a large body that parsed to nothing. Its own counter
    # *alongside* the two above, not instead of them - a blocked source is
    # neither "the site errored" (consecutive_failures) nor "the site was
    # quiet" (consecutive_empty_runs), and conflating it with either is what
    # lets a failed workaround go unnoticed. Reset only by a fetch that came
    # back clean.
    consecutive_blocked_runs = Column(Integer, default=0)
    last_blocked_reason = Column(Text)
    last_blocked_at = Column(DateTime)


class ItemArtefactRow(Base):
    __tablename__ = "item_artefacts"

    item_id = Column(String, primary_key=True)
    path = Column(String, nullable=False)  # relative to ARTEFACTS_DIR
    mime = Column(String, nullable=False)
    sha256 = Column(String, nullable=False)
    bytes = Column(Integer, nullable=False)
    fetched_at = Column(DateTime, nullable=False)


class ClassificationRow(Base):
    """One classification event with provenance (architecture doc §4.4).
    `items.labels`/`items.updated_at` and `item_labels` are materialised
    from the live (superseded_at IS NULL) rows here by
    regradar.classification.materialize_labels() - never written directly.
    """
    __tablename__ = "classifications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    item_id = Column(String, nullable=False)
    axis = Column(String, nullable=False)
    value = Column(String, nullable=False)
    by = Column(String, nullable=False)  # source_default | rule | llm:<provider>/<model> | human:<name> | import
    confidence = Column(Float, nullable=False)
    taxonomy_version = Column(Integer, nullable=False)
    classifier_id = Column(String)
    created_at = Column(DateTime, nullable=False)
    superseded_at = Column(DateTime, nullable=True)


Index("ix_classifications_item_axis_live", ClassificationRow.item_id, ClassificationRow.axis)


class ItemLabelRow(Base):
    """Denormalised (item_id, axis, value) index over the live effective
    labels, rebuilt on every write - the query-speed side table recommended
    in architecture doc §4.4 (see the measurement in the task report)."""
    __tablename__ = "item_labels"

    item_id = Column(String, primary_key=True)
    axis = Column(String, primary_key=True)
    value = Column(String, primary_key=True)


Index("ix_item_labels_axis_value", ItemLabelRow.axis, ItemLabelRow.value)


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
        _ensure_column(conn, "items", "labels", "TEXT")
        _ensure_column(conn, "items", "body_md", "TEXT")
        _ensure_column(conn, "items", "extractor", "TEXT")
        _ensure_column(conn, "items", "updated_at", "DATETIME")
        _ensure_column(conn, "runs", "db_integrity_ok", "BOOLEAN")
        _ensure_column(conn, "source_errors", "consecutive_empty_runs", "INTEGER DEFAULT 0")
        _ensure_column(conn, "source_errors", "consecutive_blocked_runs", "INTEGER DEFAULT 0")
        _ensure_column(conn, "source_errors", "last_blocked_reason", "TEXT")
        _ensure_column(conn, "source_errors", "last_blocked_at", "DATETIME")
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
                    "t": db_now(),
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


def record_source_empty_run(engine, regulator_id: str, source_label: str) -> None:
    """Increments consecutive_empty_runs for a source that fetched
    successfully but yielded zero items - the "200 OK, wrong content"
    selector-drift case reset_source_errors alone never catches, since no
    exception was raised. Mirrors record_source_error's upsert shape."""
    with Session(engine) as session:
        row = session.execute(
            text("SELECT id, consecutive_empty_runs FROM source_errors WHERE regulator_id=:r AND source_label=:s"),
            {"r": regulator_id, "s": source_label}
        ).fetchone()
        if row:
            session.execute(
                text(
                    "UPDATE source_errors SET consecutive_empty_runs=:c, last_attempt_at=:t "
                    "WHERE regulator_id=:r AND source_label=:s"
                ),
                {
                    "c": (row[1] or 0) + 1,
                    "t": db_now(),
                    "r": regulator_id,
                    "s": source_label,
                }
            )
        else:
            session.add(SourceErrorRow(
                regulator_id=regulator_id,
                source_label=source_label,
                consecutive_empty_runs=1,
                last_attempt_at=datetime.now(timezone.utc),
            ))
        session.commit()


def record_source_blocked(engine, regulator_id: str, source_label: str, reason: str) -> None:
    """Increments consecutive_blocked_runs for a source whose fetch was
    classified as blocked/challenged (collector.adapters.http.detect_block
    or the large-body-zero-items heuristic). Mirrors record_source_error's
    upsert shape, and shares its row - `blocked` is an additional state on
    the same source, not a replacement for the error/empty counters."""
    with Session(engine) as session:
        row = session.execute(
            text("SELECT id, consecutive_blocked_runs FROM source_errors WHERE regulator_id=:r AND source_label=:s"),
            {"r": regulator_id, "s": source_label}
        ).fetchone()
        if row:
            session.execute(
                text(
                    "UPDATE source_errors SET consecutive_blocked_runs=:c, last_blocked_reason=:e, "
                    "last_blocked_at=:t, last_attempt_at=:t WHERE regulator_id=:r AND source_label=:s"
                ),
                {
                    "c": (row[1] or 0) + 1,
                    "e": reason,
                    "t": db_now(),
                    "r": regulator_id,
                    "s": source_label,
                }
            )
        else:
            now = datetime.now(timezone.utc)
            session.add(SourceErrorRow(
                regulator_id=regulator_id,
                source_label=source_label,
                consecutive_blocked_runs=1,
                last_blocked_reason=reason,
                last_blocked_at=now,
                last_attempt_at=now,
            ))
        session.commit()


def reset_source_blocked(engine, regulator_id: str, source_label: str) -> None:
    with Session(engine) as session:
        session.execute(
            text(
                "UPDATE source_errors SET consecutive_blocked_runs=0 "
                "WHERE regulator_id=:r AND source_label=:s"
            ),
            {"r": regulator_id, "s": source_label}
        )
        session.commit()


def reset_source_empty_runs(engine, regulator_id: str, source_label: str) -> None:
    with Session(engine) as session:
        session.execute(
            text(
                "UPDATE source_errors SET consecutive_empty_runs=0 "
                "WHERE regulator_id=:r AND source_label=:s"
            ),
            {"r": regulator_id, "s": source_label}
        )
        session.commit()


class WebhookSubscriptionRow(Base):
    """One row per configured webhook subscriber (config/webhooks.yaml),
    keyed by regradar.webhooks' stable subscriber id (derived from
    profile+url - the config has no explicit id field). `cursor` is that
    subscriber's own head of the change stream (architecture doc section
    6), advanced only after a successful delivery. A later digest feature
    (issue #13) reuses this same table under its own subscriber id
    convention, per the task ruling that both share this mechanism.
    """
    __tablename__ = "webhook_subscriptions"

    subscriber_id = Column(String, primary_key=True)
    cursor = Column(String, nullable=True)
    updated_at = Column(DateTime)


class WebhookDeliveryRow(Base):
    """Append-only delivery log - one row per attempted batch delivery
    (status "success" or "failed"), for operator visibility into retries
    and failures."""
    __tablename__ = "webhook_deliveries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    subscriber_id = Column(String, nullable=False)
    url = Column(String, nullable=False)
    status = Column(String, nullable=False)  # "success" | "failed"
    detail = Column(Text)
    delivered_at = Column(DateTime, nullable=False)


def get_webhook_cursor(engine, subscriber_id: str) -> str | None:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT cursor FROM webhook_subscriptions WHERE subscriber_id = :id"),
            {"id": subscriber_id},
        ).fetchone()
    return row[0] if row else None


def set_webhook_cursor(engine, subscriber_id: str, cursor: str) -> None:
    with Session(engine) as session:
        row = session.get(WebhookSubscriptionRow, subscriber_id)
        if row:
            row.cursor = cursor
            row.updated_at = datetime.now(timezone.utc)
        else:
            session.add(WebhookSubscriptionRow(
                subscriber_id=subscriber_id, cursor=cursor, updated_at=datetime.now(timezone.utc),
            ))
        session.commit()


def record_webhook_delivery(engine, subscriber_id: str, url: str, status: str, detail: str = "") -> None:
    with Session(engine) as session:
        session.add(WebhookDeliveryRow(
            subscriber_id=subscriber_id, url=url, status=status, detail=detail,
            delivered_at=datetime.now(timezone.utc),
        ))
        session.commit()


class DigestStateRow(Base):
    """One row per configured digest (config/digests.yaml `name`), tracking
    the same "advance only after full success" cursor semantics as
    WebhookSubscriptionRow above (issue #13's ruling: reuse the webhook
    cursor mechanism rather than a second one). `last_sent_at` is for
    operator visibility only - `last_sent_cursor` is what a missed or
    resumed run actually reads to catch up exactly."""
    __tablename__ = "digest_state"

    name = Column(String, primary_key=True)
    last_sent_cursor = Column(String, nullable=True)
    last_sent_at = Column(DateTime, nullable=True)


def get_digest_cursor(engine, name: str) -> str | None:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT last_sent_cursor FROM digest_state WHERE name = :name"),
            {"name": name},
        ).fetchone()
    return row[0] if row else None


class SyncStateRow(Base):
    """A small generic key/value store for one-off sync bookkeeping that
    doesn't warrant its own table - currently just the change-stream cursor
    `reg-radar import-dataset` (regradar/dataset.py) records for the
    published dataset's `generated_at`.

    It marks where the imported snapshot ends; it does NOT filter what the
    collector fetches, and nothing consumes it automatically. A collection
    run reads whatever is on each source's current feed pages either way,
    and `reg-radar export` always starts full into a fresh --out. It is a
    bookmark for change-stream consumers - get_changes()/`/api/changes`/a
    webhook subscriber - that want only what arrived after the snapshot,
    surfaced by get_health()'s `dataset_import`. Distinct from
    `webhook_subscriptions`, which is keyed per outbound subscriber rather
    than per inbound sync source.
    """
    __tablename__ = "sync_state"

    key = Column(String, primary_key=True)
    value = Column(String, nullable=True)
    updated_at = Column(DateTime)


def get_sync_state(engine, key: str) -> str | None:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT value FROM sync_state WHERE key = :key"), {"key": key},
        ).fetchone()
    return row[0] if row else None


def set_digest_cursor(engine, name: str, cursor: str) -> None:
    with Session(engine) as session:
        row = session.get(DigestStateRow, name)
        if row:
            row.last_sent_cursor = cursor
            row.last_sent_at = datetime.now(timezone.utc)
        else:
            session.add(DigestStateRow(
                name=name, last_sent_cursor=cursor, last_sent_at=datetime.now(timezone.utc),
            ))
        session.commit()


def set_sync_state(engine, key: str, value: str) -> None:
    with Session(engine) as session:
        row = session.get(SyncStateRow, key)
        if row:
            row.value = value
            row.updated_at = datetime.now(timezone.utc)
        else:
            session.add(SyncStateRow(key=key, value=value, updated_at=datetime.now(timezone.utc)))
        session.commit()


# ---------------------------------------------------------------------------
# Module enable state and page_watch baselines (issue #51)
# ---------------------------------------------------------------------------

class ModuleStateRow(Base):
    """When a module started collecting, and from when it collects.

    Same table family and lifecycle as source_errors above: small, written
    by the collector, read by nothing that must exist for a run to work.

    `collect_from` is the watermark `on_enable` resolves to, recorded once
    at the transition into enabled and never advanced afterwards - NULL
    means "no lower bound" (backfill_all). It is deliberately not "the
    newest item seen last run": a watermark that chases the feed skips
    items whenever a run half-fails, which is the exact failure #51 names.

    `enabled` tracks whether the module was in the last selection, so a
    module that was switched off and back on is recognisable as a
    transition rather than as an uninterrupted run of collections. Rows
    already collected are never touched when a module is disabled.
    """
    __tablename__ = "module_state"

    regulator_id = Column(String, primary_key=True)
    module_id = Column(String)       # the modules.yaml entry that selected it ("eu/*", "uk-fca")
    on_enable = Column(String)       # new_only | backfill_from | backfill_all
    enabled = Column(Boolean, default=True)
    collect_from = Column(DateTime, nullable=True)
    first_enabled_at = Column(DateTime)
    updated_at = Column(DateTime)


class PageWatchBaselineRow(Base):
    """The last observed state of a page_watch source's watched fragment.

    Held here rather than inferred from `items` because the two answer
    different questions: `items` is what we reported to the user, this is
    what we last saw. Keeping it separate is what lets a first observation
    establish a baseline while reporting nothing, and what makes "a disable
    must not delete the baseline" a property of a row nothing deletes
    rather than of a code path nobody re-reads.

    The stored token is the item id, which for page_watch is a pure
    function of (regulator, url, content hash) - see
    collector.schema.make_page_watch_item_id. Equal id, unchanged page.
    """
    __tablename__ = "page_watch_baselines"

    regulator_id = Column(String, primary_key=True)
    source_label = Column(String, primary_key=True)
    item_id = Column(String, nullable=False)
    established_at = Column(DateTime)
    updated_at = Column(DateTime)


def regulator_has_items(engine, regulator_id: str) -> bool:
    """Whether this install has ever collected anything for a regulator.

    Used to tell "a module being switched on for the first time" from "a
    module that was already collecting before module state existed" - the
    latter being every regulator on an install upgrading through #48's
    migration, which must not be given a watermark that cuts its history.
    """
    with Session(engine) as session:
        return session.execute(
            text("SELECT 1 FROM items WHERE regulator_id = :r LIMIT 1"), {"r": regulator_id}
        ).fetchone() is not None


def get_module_enable_state(engine, regulator_id: str) -> dict | None:
    # Read through the ORM, not text(): SQLAlchemy's DateTime type is what
    # turns the stored string back into a datetime, and a raw SELECT would
    # hand the caller a string to compare timestamps against (see db_now).
    with Session(engine) as session:
        row = session.get(ModuleStateRow, regulator_id)
        if row is None:
            return None
        return {
            "regulator_id": row.regulator_id,
            "module_id": row.module_id,
            "on_enable": row.on_enable,
            "enabled": bool(row.enabled),
            "collect_from": row.collect_from,
            "first_enabled_at": row.first_enabled_at,
        }


def record_module_enabled(engine, regulator_id: str, module_id: str, on_enable: str,
                          collect_from: datetime | None) -> None:
    """Marks a module as collecting from now on, recording the watermark
    its `on_enable` resolved to. Called at the transition into enabled and
    only there - a module already collecting normally keeps the watermark
    it was given, so editing `on_enable:` later does not silently re-cut
    an install's history."""
    with Session(engine) as session:
        row = session.get(ModuleStateRow, regulator_id)
        now = datetime.now(timezone.utc)
        if row is None:
            session.add(ModuleStateRow(
                regulator_id=regulator_id, module_id=module_id, on_enable=on_enable,
                enabled=True, collect_from=collect_from, first_enabled_at=now, updated_at=now,
            ))
        else:
            row.module_id = module_id
            row.on_enable = on_enable
            row.enabled = True
            row.collect_from = collect_from
            row.first_enabled_at = row.first_enabled_at or now
            row.updated_at = now
        session.commit()


def mark_modules_disabled(engine, active_regulator_ids) -> list[str]:
    """Flags every known module that is no longer in the selection, and
    returns the ids that changed state this call (so the caller can log a
    disable once rather than every run). Deliberately only writes the flag:
    items, artefacts, classifications and page_watch baselines for a
    disabled module all stay exactly where they are."""
    active = set(active_regulator_ids)
    with Session(engine) as session:
        rows = session.query(ModuleStateRow).filter(ModuleStateRow.enabled.is_(True)).all()
        newly_disabled = [r.regulator_id for r in rows if r.regulator_id not in active]
        for row in rows:
            if row.regulator_id not in active:
                row.enabled = False
                row.updated_at = datetime.now(timezone.utc)
        session.commit()
    return newly_disabled


def get_page_watch_baseline(engine, regulator_id: str, source_label: str) -> str | None:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT item_id FROM page_watch_baselines "
                "WHERE regulator_id = :r AND source_label = :s"
            ),
            {"r": regulator_id, "s": source_label},
        ).fetchone()
    return row[0] if row else None


def set_page_watch_baseline(engine, regulator_id: str, source_label: str, item_id: str) -> None:
    with Session(engine) as session:
        row = session.get(PageWatchBaselineRow, (regulator_id, source_label))
        now = datetime.now(timezone.utc)
        if row is None:
            session.add(PageWatchBaselineRow(
                regulator_id=regulator_id, source_label=source_label, item_id=item_id,
                established_at=now, updated_at=now,
            ))
        else:
            row.item_id = item_id
            row.updated_at = now
        session.commit()


def latest_item_id(engine, regulator_id: str, source_label: str) -> str | None:
    """The most recently fetched item id for a source, used only to adopt a
    baseline for a page_watch source that already has collection history
    from before baselines existed. Without it the upgrade run would treat
    a long-watched page as brand new and swallow a genuine change."""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT id FROM items WHERE regulator_id = :r AND source_label = :s "
                "ORDER BY fetched_at DESC LIMIT 1"
            ),
            {"r": regulator_id, "s": source_label},
        ).fetchone()
    return row[0] if row else None

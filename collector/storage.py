import json
import uuid
from datetime import datetime, timezone

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
    tags = Column(Text)  # JSON array
    raw_content = Column(Text)


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


class SourceErrorRow(Base):
    __tablename__ = "source_errors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    regulator_id = Column(String, nullable=False)
    source_label = Column(String, nullable=False)
    consecutive_failures = Column(Integer, default=0)
    last_error = Column(Text)
    last_attempt_at = Column(DateTime)


def init_db(db_path: str):
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"timeout": 30})
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.execute(text("PRAGMA synchronous=NORMAL"))
        conn.commit()
    Base.metadata.create_all(engine)
    return engine


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
            tags=json.dumps(item.tags),
            raw_content=item.raw_content,
        )
        session.add(row)
        session.commit()
        return True


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


def record_run_complete(engine, run_id: str, total_fetched: int, total_new: int, total_errors: int, error_detail: list, status: str | None = None) -> None:
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

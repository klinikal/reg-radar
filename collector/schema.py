import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


def make_item_id(regulator_id: str, url: str, published_at: Optional[datetime]) -> str:
    if published_at is not None:
        raw = f"{regulator_id}|{url}|{published_at.isoformat()}"
    else:
        raw = f"{regulator_id}|{url}"
    return hashlib.sha256(raw.encode()).hexdigest()


def make_page_watch_item_id(regulator_id: str, url: str, content_hash: str) -> str:
    raw = f"{regulator_id}|{url}|{content_hash}"
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass
class Item:
    id: str
    regulator_id: str
    source_label: str
    title: str
    url: str
    published_at: Optional[datetime]
    fetched_at: datetime
    date_inferred: bool
    language: str
    content_snippet: str
    source_type: str
    source_tags: list[str] = field(default_factory=list)
    content_tags: list[str] = field(default_factory=list)
    # Transient, not persisted: {(axis, value): confidence} computed by the
    # rules engine (regradar.rules.RulesEngine) in collector.pipeline.classify()
    # - written as `by: rule` classifications once the item is inserted (see
    # collector.main.run_collection), same as source-default labels.
    rule_labels: dict = field(default_factory=dict)
    raw_content: str = ""
    body_md: str = ""
    extractor: str = ""
    raw_bytes: Optional[bytes] = None
    mime: Optional[str] = None

import hashlib
from datetime import datetime, timezone
from collector.schema import Item, make_item_id


def test_item_id_with_published_at():
    published = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
    item_id = make_item_id("esma", "https://example.com/item", published)
    expected = hashlib.sha256(
        f"esma|https://example.com/item|{published.isoformat()}".encode()
    ).hexdigest()
    assert item_id == expected


def test_item_id_without_published_at():
    item_id = make_item_id("esma", "https://example.com/item", None)
    expected = hashlib.sha256(
        "esma|https://example.com/item".encode()
    ).hexdigest()
    assert item_id == expected


def test_item_dataclass_fields():
    now = datetime.now(timezone.utc)
    item = Item(
        id="abc123",
        regulator_id="esma",
        source_label="all_news",
        title="Test item",
        url="https://example.com",
        published_at=now,
        fetched_at=now,
        date_inferred=False,
        language="en",
        content_snippet="First 500 chars...",
        source_type="rss",
        tags=["casp", "mica"],
        raw_content="<p>Full content</p>",
    )
    assert item.regulator_id == "esma"
    assert item.tags == ["casp", "mica"]

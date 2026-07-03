import hashlib
from datetime import datetime, timezone
from collector.schema import Item, make_item_id, make_page_watch_item_id


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
        source_tags=["casp", "mica"],
        raw_content="<p>Full content</p>",
    )
    assert item.regulator_id == "esma"
    assert item.source_tags == ["casp", "mica"]


def test_make_page_watch_item_id_is_deterministic():
    id1 = make_page_watch_item_id("esma", "https://example.com/page", "abc123")
    id2 = make_page_watch_item_id("esma", "https://example.com/page", "abc123")
    assert id1 == id2


def test_make_page_watch_item_id_differs_on_content_hash_change():
    id1 = make_page_watch_item_id("esma", "https://example.com/page", "abc123")
    id2 = make_page_watch_item_id("esma", "https://example.com/page", "def456")
    assert id1 != id2


def test_make_page_watch_item_id_never_collides_with_make_item_id():
    from datetime import datetime, timezone
    published_at = datetime(2026, 7, 3, tzinfo=timezone.utc)
    regular_id = make_item_id("esma", "https://example.com/page", published_at)
    # A content hash is a 64-hex-char sha256 digest; an ISO-8601 timestamp
    # string is a different shape entirely, so these two id schemes can
    # never produce the same input string for sha256, even for the same
    # (regulator_id, url) pair.
    watch_id = make_page_watch_item_id("esma", "https://example.com/page", "a" * 64)
    assert regular_id != watch_id


def test_item_has_source_tags_and_content_tags_fields():
    from datetime import datetime, timezone
    item = Item(
        id="x", regulator_id="esma", source_label="all_news", title="t", url="https://x",
        published_at=datetime.now(timezone.utc), fetched_at=datetime.now(timezone.utc),
        date_inferred=False, language="en", content_snippet="s", source_type="rss",
        source_tags=["warnings"], content_tags=["mica"],
    )
    assert item.source_tags == ["warnings"]
    assert item.content_tags == ["mica"]


def test_item_content_tags_defaults_to_empty_list():
    from datetime import datetime, timezone
    item = Item(
        id="x", regulator_id="esma", source_label="all_news", title="t", url="https://x",
        published_at=datetime.now(timezone.utc), fetched_at=datetime.now(timezone.utc),
        date_inferred=False, language="en", content_snippet="s", source_type="rss",
        source_tags=["warnings"],
    )
    assert item.content_tags == []

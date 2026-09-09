from datetime import datetime, timezone

import pytest

from regradar.changes import CursorError, decode_cursor, encode_cursor


def test_round_trip_datetime():
    dt = datetime(2026, 8, 20, 6, 0, 3, 123456, tzinfo=timezone.utc)
    cursor = encode_cursor(dt, "item-1")
    assert cursor == "2026-08-20T06:00:03.123456Z|item-1"
    decoded_dt, decoded_id = decode_cursor(cursor)
    assert decoded_dt == dt
    assert decoded_id == "item-1"


def test_round_trip_naive_datetime_assumed_utc():
    dt = datetime(2026, 8, 20, 6, 0, 3, 123456)
    cursor = encode_cursor(dt, "item-1")
    assert cursor == "2026-08-20T06:00:03.123456Z|item-1"


def test_round_trip_z_string():
    cursor = encode_cursor("2026-08-20T06:00:03.123456Z", "item-2")
    dt, item_id = decode_cursor(cursor)
    assert dt == datetime(2026, 8, 20, 6, 0, 3, 123456, tzinfo=timezone.utc)
    assert item_id == "item-2"


def test_round_trip_sqlite_format_string_with_microseconds():
    # SQLAlchemy's SQLite DATETIME storage format - see mcpserver.queries._format_dt.
    cursor = encode_cursor("2026-08-20 06:00:03.123456", "item-3")
    dt, item_id = decode_cursor(cursor)
    assert dt == datetime(2026, 8, 20, 6, 0, 3, 123456, tzinfo=timezone.utc)
    assert item_id == "item-3"


def test_item_id_with_pipe_character_still_splits_on_first_pipe():
    cursor = encode_cursor(datetime(2026, 1, 1, tzinfo=timezone.utc), "id|with|pipes")
    dt, item_id = decode_cursor(cursor)
    assert item_id == "id|with|pipes"


@pytest.mark.parametrize("bad", ["", "no-separator", "|missing-timestamp", "not-a-date|item"])
def test_decode_rejects_malformed_cursors(bad):
    with pytest.raises(CursorError):
        decode_cursor(bad)


def test_encode_rejects_empty_item_id():
    with pytest.raises(CursorError):
        encode_cursor(datetime(2026, 1, 1, tzinfo=timezone.utc), "")

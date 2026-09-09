"""Opaque sync cursor for the change-stream primitive (architecture doc
section 6): `<updated_at ISO 8601 Z>|<item_id>`. This is the one encode/
decode point - mcpserver.queries.get_changes, get_changes_tool,
webui.app's /api/changes, regradar.exporter and regradar.webhooks all treat
the return value of encode_cursor() as opaque and never build or parse one
by hand.

Microsecond precision (matching the database's own storage precision, not
regradar.document's whole-second frontmatter rendering) is deliberate:
item ids are content hashes, not monotonic, so two items updated within the
same whole second - routine during one collection run - would otherwise
collide on the id tiebreak and one could be silently skipped forever. The
issue's "id as tiebreak on equal timestamps" is still honoured exactly -
ties just become the rare case they should be, rather than routine.
"""
from datetime import datetime, timezone

_CURSOR_TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


class CursorError(ValueError):
    """Raised when a cursor string doesn't parse: missing separator, an
    unparsable timestamp, or an empty item id."""


def _to_utc(value) -> datetime:
    if isinstance(value, str):
        raw = value.replace("T", " ")
        if raw.endswith("Z"):
            raw = raw[:-1]
        value = datetime.fromisoformat(raw)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def encode_cursor(updated_at, item_id: str) -> str:
    """`updated_at` accepts a datetime, an ISO string (Z-suffixed or not),
    or a plain SQLite-format string (as raw SQL returns DATETIME
    columns)."""
    if not item_id:
        raise CursorError("encode_cursor: item_id is required")
    dt = _to_utc(updated_at)
    return f"{dt.strftime(_CURSOR_TS_FORMAT)}|{item_id}"


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    if not cursor or "|" not in cursor:
        raise CursorError(f"malformed cursor: {cursor!r}")
    ts_part, item_id = cursor.split("|", 1)
    if not item_id:
        raise CursorError(f"malformed cursor: {cursor!r}")
    try:
        dt = datetime.strptime(ts_part, _CURSOR_TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError as e:
        raise CursorError(f"malformed cursor timestamp: {ts_part!r}") from e
    return dt, item_id

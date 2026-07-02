from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from collector.chunking import chunk_text, content_hash
from collector.storage import ItemChunkRow


def run_chunking(engine) -> tuple[int, int]:
    items_chunked = 0
    items_skipped = 0
    with Session(engine) as session:
        rows = session.execute(text("SELECT id, raw_content, content_snippet FROM items")).fetchall()
        for item_id, raw_content, content_snippet in rows:
            source_text = raw_content or content_snippet or ""
            new_hash = content_hash(source_text)
            existing = session.execute(
                text("SELECT content_hash FROM item_chunks WHERE item_id=:id LIMIT 1"),
                {"id": item_id},
            ).fetchone()
            if existing and existing[0] == new_hash:
                items_skipped += 1
                continue

            session.execute(text("DELETE FROM item_chunks WHERE item_id=:id"), {"id": item_id})
            for i, chunk in enumerate(chunk_text(source_text)):
                session.add(ItemChunkRow(
                    id=f"{item_id}:{i}",
                    item_id=item_id,
                    chunk_index=i,
                    text=chunk,
                    token_count=len(chunk.split()),
                    content_hash=new_hash,
                    created_at=datetime.now(timezone.utc),
                ))
            items_chunked += 1
        session.commit()
    return items_chunked, items_skipped

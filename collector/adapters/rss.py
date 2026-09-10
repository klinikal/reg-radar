"""RSS/Atom adapter - wraps v1's `_parse_rss` unchanged (feedparser)."""
from datetime import datetime, timezone

import feedparser

from collector.adapters.base import FetchContext, RawDocument, SourceAdapter, parse_text_date
from collector.adapters.http import fetch_bytes


def _entry_language(entry: dict) -> str | None:
    """An entry's own declared language (its <language> field, or a
    <category>/<tag> whose scheme ends "lang") - takes precedence over the
    source's configured preferred_language. Rare in practice but preserved
    from v1's _resolve_language()."""
    if entry.get("language"):
        return entry["language"]
    for tag in entry.get("tags", []) or []:
        if hasattr(tag, "term") and (tag.get("scheme") or "").endswith("lang"):
            return tag.term
    return None


class RssAdapter(SourceAdapter):
    type_id = "rss"

    def fetch(self, source_cfg: dict, ctx: FetchContext) -> list[RawDocument]:
        content = self.fetch_raw(source_cfg, ctx)
        return self.parse(content, source_cfg, ctx.regulator_id)

    def fetch_raw(self, source_cfg: dict, ctx: FetchContext) -> bytes:
        content = fetch_bytes(
            source_cfg["url"], ctx.user_agent, ctx.timeout, ctx.retries,
            flaresolverr_url=ctx.flaresolverr_url,
        )
        if content is None:
            raise RuntimeError(f"Failed to fetch {source_cfg['url']} for {ctx.regulator_id}/{source_cfg['label']}")
        return content

    def parse(self, content: bytes | str, source_cfg: dict, regulator_id: str) -> list[RawDocument]:
        feed = feedparser.parse(content)
        docs = []
        for entry in feed.entries:
            title = entry.get("title", "").strip()
            url = entry.get("link", "").strip()
            if not title or not url:
                continue

            published_at = None
            date_inferred = False
            if entry.get("published_parsed"):
                try:
                    published_at = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                except Exception:
                    pass
            if published_at is None:
                # feedparser fills published_parsed only for dates it reads as
                # RFC 822 / ISO 8601. A feed that renders its date for humans
                # still carries the string, and dropping it silently gives
                # every item a fetch-time date with date_inferred=True. The
                # FCA's news and warnings feeds do exactly that - they publish
                # "Thursday, September 3, 2026 - 10:35" - so every item from
                # the UK's primary regulator was dated by when it was
                # collected rather than when it was published (#67).
                for key in ("published", "updated", "created"):
                    raw = entry.get(key)
                    if raw:
                        published_at = parse_text_date(str(raw))
                        if published_at is not None:
                            break
            if published_at is None:
                date_inferred = True

            summary_html = entry.get("summary", "") or entry.get("description", "") or ""

            docs.append(RawDocument(
                url=url,
                title=title,
                published_at=published_at,
                date_inferred=date_inferred,
                content_html=summary_html,
                is_partial=True,
                language=_entry_language(entry),
            ))
        return docs

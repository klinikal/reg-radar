"""JSON API adapter: fetches JSON, maps fields via dotted paths.

Config block:

    sources:
      - type: json_api
        url: https://example.com/api/publications
        label: publications
        json_api:
          items_path: data.items       # dotted path to the list of items; "" means the response itself is the list
          title_field: title
          url_field: link
          published_field: date        # optional

A minimal dotted-path resolver (attribute/key lookup only) is deliberately
all this supports - see the issue's open questions on whether real JSONPath
(array filters, wildcards) is ever needed.
"""
import logging
from datetime import timezone
from urllib.parse import urljoin

from dateutil.parser import parse as parse_date

from collector.adapters.base import FetchContext, RawDocument, SourceAdapter
from collector.adapters.http import fetch_bytes

logger = logging.getLogger(__name__)


def resolve_path(data, path: str):
    """Walks `path` ("a.b.c") through nested dicts/lists. A list segment
    that isn't a valid index is skipped (returns None) rather than raising -
    a malformed/missing field on one item shouldn't crash the whole fetch.
    """
    if not path:
        return data
    current = data
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current


class JsonApiAdapter(SourceAdapter):
    type_id = "json_api"

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
        import json

        api_cfg = source_cfg.get("json_api", {})
        items_path = api_cfg.get("items_path", "")
        title_field = api_cfg.get("title_field", "title")
        url_field = api_cfg.get("url_field", "url")
        published_field = api_cfg.get("published_field")

        try:
            data = json.loads(content)
        except (ValueError, TypeError) as e:
            logger.error("json_api source %s/%s: invalid JSON - %s", regulator_id, source_cfg["label"], e)
            return []

        raw_items = resolve_path(data, items_path)
        if not isinstance(raw_items, list):
            logger.error(
                "json_api source %s/%s: items_path %r did not resolve to a list",
                regulator_id, source_cfg["label"], items_path,
            )
            return []

        docs = []
        for raw_item in raw_items:
            title = resolve_path(raw_item, title_field)
            url = resolve_path(raw_item, url_field)
            if not title or not url:
                continue

            published_at = None
            date_inferred = True
            if published_field:
                raw_date = resolve_path(raw_item, published_field)
                if raw_date:
                    try:
                        parsed = parse_date(str(raw_date))
                        published_at = parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
                        date_inferred = False
                    except (ValueError, OverflowError):
                        pass

            docs.append(RawDocument(
                # Several APIs return relative paths only - MAS's Solr index has no
                # absolute-URL field at all. urljoin returns an already-absolute URL
                # unchanged, so absolute-URL sources need no branching.
                url=urljoin(source_cfg.get("url", ""), str(url)),
                title=str(title),
                published_at=published_at,
                date_inferred=date_inferred,
            ))
        return docs

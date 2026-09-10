"""Shared post-fetch pipeline
(../wiki/content/concept-source-adapter-framework.md): turns adapter-agnostic `RawDocument`s into
`collector.schema.Item`s. Everything here used to be duplicated across
`_parse_rss`/`_parse_scrape`/`_parse_page_watch` in collector/fetcher.py -
id generation, source-tag filtering, language resolution, snippet
truncation and body extraction now live once.
"""
import logging
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from collector.adapters.base import RawDocument
from collector.language import detect_language
from collector.schema import Item, make_item_id, make_page_watch_item_id
from regradar.document import convert_body, rss_body_markdown

logger = logging.getLogger(__name__)

# Blanket regulation/instrument-name labels applied uniformly to every item
# from a source regardless of what that specific item actually says (e.g.
# every AMLA item gets "aml", every EBA item gets "art"/"emt") - stripped
# from source_tags so a tag isn't rendered meaningless by being on nearly
# everything. Everything else in a source's config tags (warnings,
# press_releases, sanctions, enforcement, ...) genuinely describes the
# notice's kind and is kept.
BLANKET_LABEL_DENYLIST = frozenset({"casp", "mica", "aml", "art", "emt", "dora"})


def filter_source_tags(tags: list[str]) -> list[str]:
    return [t for t in tags if t not in BLANKET_LABEL_DENYLIST]


def truncate_snippet(text: str) -> str:
    return text[:500]


def resolve_language(override: str | None, source_preferred: str | None, title: str, snippet: str) -> str:
    """override is an adapter-precomputed value (e.g. an RSS entry's own
    <language> tag or lang-scheme <category>) - it wins outright; otherwise
    the source's configured preferred_language, then free-text detection.
    """
    if override:
        return override
    if source_preferred:
        return source_preferred
    return detect_language(f"{title} {snippet}")


def classify(item: Item, raw_doc: RawDocument, rules_engine=None) -> None:
    """The rules engine's call-out point (issue #6, config/rules.yaml):
    scores this item's title/raw_content against `rules_engine` (a
    regradar.rules.RulesEngine) and stores the result on `item.rule_labels`.

    Scoring only - never writes to the database, since the item doesn't
    exist in it yet at this point in the pipeline. collector.main writes
    `item.rule_labels` as `by: rule` classifications once insert_item()
    succeeds, the same way source-default labels are written from
    `item`/`source` data after insertion. `rules_engine=None` (the default,
    matching every caller that predates this issue) is a no-op, matching
    the old behaviour.
    """
    if rules_engine is not None:
        item.rule_labels = rules_engine.score(item.title, item.raw_content)
    return None


def _extract(raw: RawDocument) -> tuple[str, str]:
    """Returns (raw_content, snippet). RSS keeps the raw, unconverted
    summary and a non-strip get_text() snippet (v1's _parse_rss); everything
    else uses a stripped get_text(), content_html taking priority over
    pdf_text over content_text - mutually exclusive in practice (an adapter
    sets at most one), matching `convert_body`'s own priority so raw_content
    always reflects whatever body_md was actually built from.
    """
    if raw.is_partial:
        raw_content = raw.content_html or ""
        snippet = truncate_snippet(BeautifulSoup(raw_content, "lxml").get_text()) if raw_content else ""
        return raw_content, snippet
    if raw.content_html:
        raw_content = BeautifulSoup(raw.content_html, "lxml").get_text(strip=True)
        return raw_content, truncate_snippet(raw_content)
    if raw.pdf_text is not None:
        return raw.pdf_text, truncate_snippet(raw.pdf_text)
    if raw.content_text is not None:
        return raw.content_text, truncate_snippet(raw.content_text)
    return "", ""


def _body_and_extractor(raw: RawDocument, snippet: str) -> tuple[str, str]:
    """Returns (body_md, extractor) - the "extract" step of the shared
    pipeline (../wiki/content/concept-source-adapter-framework.md)."""
    if raw.is_partial:
        # RSS/Atom: never a fetched full document, only the feed's own
        # summary - rss_body_markdown always appends the "not collected"
        # note, matching v1's _parse_rss behaviour exactly.
        return rss_body_markdown(raw.content_html, snippet, raw.url)
    return convert_body(
        content_html=raw.content_html, pdf_text=raw.pdf_text, plain_text=raw.content_text,
        snippet=snippet, source_url=raw.url,
    )


def process_raw_documents(raw_docs: list[RawDocument], source: dict, regulator_id: str, rules_engine=None) -> list[Item]:
    """The shared "extract, hash, ..." half of the pipeline (storage and the
    classify call-out happen in collector.main.run_collection, which already
    owns insert_item()/_apply_source_label_defaults() per-item decisions).

    No `config` (user_agent/timeout/retries) param - those are fetch-time
    concerns the adapter already consumed in fetch()/fetch_raw(); nothing
    here needs them.
    """
    now = datetime.now(timezone.utc)
    source_type = source.get("type", "rss")
    source_label = source["label"]
    source_preferred_lang = source.get("preferred_language")
    tags = filter_source_tags(source.get("tags", []))

    items = []
    for raw in raw_docs:
        # Trust the adapter's own published_at/date_inferred pair as final -
        # v1 had inconsistent per-type policy here (rss/scrape null
        # published_at once inferred; page_watch keeps it at fetched_at) and
        # this preserves that rather than "fixing" fetched output (issue
        # #8's non-goals).
        published_at = raw.published_at
        raw_content, snippet = _extract(raw)
        body_md, extractor = _body_and_extractor(raw, snippet)
        language = resolve_language(raw.language, source_preferred_lang, raw.title, snippet)

        if source_type == "page_watch":
            item_id = make_page_watch_item_id(regulator_id, raw.url, raw.content_hash or "")
        else:
            item_id = make_item_id(regulator_id, raw.url, published_at)

        item = Item(
            id=item_id,
            regulator_id=regulator_id,
            source_label=source_label,
            title=raw.title,
            url=raw.url,
            published_at=published_at,
            fetched_at=now,
            date_inferred=raw.date_inferred,
            language=language,
            content_snippet=snippet,
            source_type=source_type,
            source_tags=tags,
            content_tags=[],
            raw_content=raw_content,
            body_md=body_md,
            extractor=extractor,
            raw_bytes=raw.body if raw.body else None,
            mime=raw.mime if raw.body else None,
        )
        classify(item, raw, rules_engine=rules_engine)
        items.append(item)
    return items

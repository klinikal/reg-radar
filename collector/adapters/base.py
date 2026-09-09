"""Adapter output contract (docs/issues/08-source-adapter-framework-registry-split.md).

`RawDocument` is what every adapter's `fetch()` returns; `collector.pipeline`
turns a list of them into `collector.schema.Item` rows. The fields beyond the
issue's base shape (url, title, published_at, body, mime, labels,
date_inferred) exist because "the original response to store as an
artefact" and "the content used to build the Markdown body" are not always
the same fetch - a page_watch artefact is the *whole* fetched page, but its
body/content comes from one CSS-selected fragment of it, and an RSS/Atom
entry has no fetched artefact at all, only the feed's own summary. Keeping
these apart lets every adapter's post-fetch handling (extraction, snippet,
language, id generation) run through one shared pipeline instead of each
adapter reimplementing it.
"""
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class RawDocument:
    url: str
    title: str
    published_at: datetime | None
    body: bytes = b""
    mime: str = ""
    labels: dict = field(default_factory=dict)
    date_inferred: bool = False

    # Content for the shared pipeline to build body_md/raw_content from -
    # distinct from body/mime (the artefact to store), see module docstring.
    # Exactly one of these is normally set; none set means "no content beyond
    # the snippet fallback" (docs/design/regradar-architecture.md section 3.2).
    content_html: str | None = None
    content_text: str | None = None
    pdf_text: str | None = None
    # True for a feed-supplied summary (RSS/Atom) - never the full document,
    # so the pipeline always appends the "not collected" note regardless of
    # whether content_html happened to parse to something.
    is_partial: bool = False

    # page_watch identifies a "changed" item by content hash, not by date -
    # set only by that adapter; the pipeline uses it instead of published_at
    # for item id generation when present.
    content_hash: str | None = None

    # Pre-resolved language override (e.g. an RSS entry's own <language> tag
    # or lang-scheme <category>) - takes precedence over the pipeline's
    # source_preferred_language/detection fallback. None means "let the
    # pipeline resolve it".
    language: str | None = None


@dataclass
class FetchContext:
    user_agent: str
    timeout: int
    retries: int
    flaresolverr_url: str | None
    regulator_id: str


class SourceAdapter:
    """Base class for a `sources[].type` handler. Subclasses set `type_id`
    and implement `fetch()`; register an instance in
    `collector.adapters.registry.REGISTRY`.
    """

    type_id: str

    def fetch(self, source_cfg: dict, ctx: FetchContext) -> list[RawDocument]:
        raise NotImplementedError

    def fetch_raw(self, source_cfg: dict, ctx: FetchContext):
        """The network half of fetch(), without parsing - the raw response
        (bytes/str, whatever `parse()` accepts). Split out so fixture
        capture and the nightly live check (scripts/source_health.py) can
        get the real response to write/diff without going through parse()
        twice. Not every adapter type needs a fixture-capturable raw form
        (browser's "raw" is a rendered DOM, not a network response) - those
        may leave this raising NotImplementedError.
        """
        raise NotImplementedError

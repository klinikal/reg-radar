"""Adapter output contract (../../wiki/content/concept-source-adapter-framework.md).

`RawDocument` is what every adapter's `fetch()` returns; `collector.pipeline`
turns a list of them into `collector.schema.Item` rows. The fields beyond the
issue's base shape (url, title, published_at, body, mime,
date_inferred) exist because "the original response to store as an
artefact" and "the content used to build the Markdown body" are not always
the same fetch - a page_watch artefact is the *whole* fetched page, but its
body/content comes from one CSS-selected fragment of it, and an RSS/Atom
entry has no fetched artefact at all, only the feed's own summary. Keeping
these apart lets every adapter's post-fetch handling (extraction, snippet,
language, id generation) run through one shared pipeline instead of each
adapter reimplementing it.
"""
import re
from dataclasses import dataclass
from datetime import datetime, timezone


_ISO_DATE = re.compile(r"\s*\d{4}-\d{1,2}-\d{1,2}")


def parse_text_date(date_str: str, fuzzy: bool = False, date_attribute: str | None = None) -> datetime | None:
    """Parse a date a source rendered for humans, returning UTC or None.

    Shared by every adapter that reads a date out of text rather than a
    machine-readable field, so these two rules hold everywhere rather than
    per-adapter:

    dayfirst - EEA/UK sources render free text as DD/MM/YYYY, so 02/07/2026
    must be 2 July, not 7 February. Not applied when the value came from a
    machine-readable attribute, which is already unambiguous.

    ISO text is exempt from dayfirst, because dayfirst corrupts it: dateutil
    reads "2026-08-03" as 8 March and "2026-12-01" as 12 January, and the
    result is stored with date_inferred=False - asserted as fact. Keyed off
    the string's shape, so any ISO-text source is right by default.

    fuzzy is NOT a safe default: it extracts a date-shaped substring from
    surrounding prose and never fails loudly. Opt in per source.
    """
    try:
        from dateutil.parser import parse as _parse
        iso_text = _ISO_DATE.match(date_str) is not None
        parsed = _parse(date_str, fuzzy=fuzzy, dayfirst=not date_attribute and not iso_text)
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


@dataclass
class RawDocument:
    url: str
    title: str
    published_at: datetime | None
    body: bytes = b""
    mime: str = ""
    date_inferred: bool = False
    # Deviation from the issue's base shape: no `labels` field. Source
    # defaults are applied from `sources[].labels` in the regulator config
    # by the classify step (collector/main.py), not carried per document,
    # so an adapter never has one to set.

    # Content for the shared pipeline to build body_md/raw_content from -
    # distinct from body/mime (the artefact to store), see module docstring.
    # Exactly one of these is normally set; none set means "no content beyond
    # the snippet fallback" (../../wiki/content/concept-canonical-document-format.md).
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

"""The canonical document format: frontmatter + Markdown body.

Renders an item (plus its optional artefact, labels and label provenance)
to the file described in docs/design/regradar-architecture.md section 3,
and parses one such file back into a frontmatter dict and a body string.
This module does no database or filesystem I/O - see regradar/render.py
for the CLI command that loads items and writes files.

`item` here is a plain dict already carrying the fields the frontmatter
needs, including the regulator's name and jurisdiction (joined in by the
caller) - see build_frontmatter() for the exact keys read.

markdownify is the only importer of the markdownify library in this
codebase; swap html_to_markdown()'s implementation to change library
without touching any caller.
"""
import hashlib
import importlib.metadata
import re
from datetime import date, datetime, timezone
from pathlib import Path

import markdownify
import yaml

SCHEMA_VERSION = 1

_SLUG_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_BLANK_RUN = re.compile(r"\n{3,}")
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n\n?(.*)\Z", re.DOTALL)


def _content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode()).hexdigest()


def _library_version(package: str) -> str:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def slugify(title: str) -> str:
    """Lower-cased, ASCII-folded, non-alphanumerics collapsed to single
    hyphens, truncated to 80 characters (docs/design/regradar-architecture.md
    section 3.1). A title that folds away to nothing (e.g. Greek or
    Bulgarian script, which has no ASCII-compatibility decomposition) falls
    back to "untitled" - document_path() still disambiguates via the id8
    suffix, so this never collides.
    """
    import unicodedata

    if not title:
        return "untitled"
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_NON_ALNUM.sub("-", ascii_title.lower()).strip("-")
    slug = slug[:80].strip("-")
    return slug or "untitled"


def _parse_dt(value) -> datetime:
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def document_path(item: dict) -> Path:
    """export/<jurisdiction>/<regulator_id>/<yyyy>/<yyyy-mm-dd>-<slug>-<id8>.md

    Items with no published_at use fetched_at (section 3.1); item["id"]
    must be present, item["fetched_at"] must be present when
    published_at is None. Accepts either datetime objects or the ISO
    strings raw SQL (regradar.render, mcpserver.queries) returns.
    """
    jurisdiction = (item.get("jurisdiction") or "unknown").lower()
    regulator_id = item["regulator_id"]
    dt = _parse_dt(item.get("published_at") or item["fetched_at"])
    slug = slugify(item.get("title") or "")
    id8 = item["id"][:8]
    return Path("export") / jurisdiction / regulator_id / f"{dt.year:04d}" / f"{dt:%Y-%m-%d}-{slug}-{id8}.md"


def html_to_markdown(html: str) -> str:
    """HTML body to Markdown: headings, paragraphs, lists, links and simple
    tables preserved; script/style stripped. Empty input returns "".
    """
    if not html or not html.strip():
        return ""
    md = markdownify.markdownify(html, heading_style="ATX", bullets="-", strip=["script", "style"])
    return _BLANK_RUN.sub("\n\n", md).strip()


def normalize_pdf_text(text: str) -> str:
    """pypdf's extract_text() (collector.adapters.http.extract_pdf_text) returns
    one line per visual line with no font-size information, so heading
    detection by font size (per the design doc) isn't possible from it -
    every line is treated as body text and only paragraph breaks are
    normalised.
    # ASSUMPTION: heading detection deferred until PDF extraction exposes per-run font size; revisit then.
    """
    if not text or not text.strip():
        return ""
    paragraphs = re.split(r"\n\s*\n", text.strip())
    normalized = []
    for para in paragraphs:
        lines = [ln.strip() for ln in para.splitlines() if ln.strip()]
        if lines:
            normalized.append(" ".join(lines))
    return "\n\n".join(normalized)


def rss_body_markdown(html_summary: str | None, snippet: str, source_url: str) -> tuple[str, str]:
    """RSS/Atom entries never carry a fetched full-text body in this
    collector, only the feed's own summary - section 3.2's rule applies
    unconditionally: the body is that summary (as Markdown) followed by the
    "not collected" note. Returns (body_md, extractor).
    """
    converted = html_to_markdown(html_summary) if html_summary else (snippet or "")
    note = f"> Full text not collected. See {source_url}."
    body = f"{converted}\n\n{note}".strip() if converted else note
    return body, f"markdownify/{_library_version('markdownify')}"


def convert_body(
    *,
    content_html: str | None = None,
    pdf_text: str | None = None,
    plain_text: str | None = None,
    snippet: str = "",
    source_url: str = "",
) -> tuple[str, str]:
    """Body conversion for scrape/page_watch items: prefer an HTML fragment
    (markdownify), then extracted PDF text (paragraph normalisation), then
    already-flattened plain text, then the "not collected" fallback.
    Returns (body_md, extractor).
    """
    if content_html and content_html.strip():
        return html_to_markdown(content_html), f"markdownify/{_library_version('markdownify')}"
    if pdf_text and pdf_text.strip():
        return normalize_pdf_text(pdf_text), f"pypdf/{_library_version('pypdf')}"
    if plain_text and plain_text.strip():
        return plain_text.strip(), "raw/1"
    note = f"> Full text not collected. See {source_url}."
    body = f"{snippet}\n\n{note}".strip() if snippet else note
    return body, "raw/1"


def _as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if isinstance(value, datetime):
        return value.date()
    return value


def _as_utc_datetime(value) -> datetime | None:
    """Accepts a datetime or an ISO string - raw SQL (used by
    regradar.render and mcpserver.queries) returns SQLite's stored
    datetime columns as plain strings, not python datetime objects.
    Microseconds are dropped - the frontmatter's declared precision
    (section 3.2's examples) is whole seconds, and both build_frontmatter()
    and parse() need to agree on that precision for the round trip to be
    lossless at the field's own resolution.
    """
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.replace(microsecond=0)


_DATETIME_Z_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _format_datetime_z(value: datetime) -> str:
    """ISO 8601 with a T separator and Z suffix for UTC, per section 3.2's
    frontmatter example (e.g. 2026-08-15T06:02:11Z) - PyYAML's own
    datetime representer instead renders "2026-08-15 06:02:11+00:00"
    (space-separated, explicit offset), so fetched_at/updated_at are
    formatted explicitly rather than left to the default representer.
    """
    return value.astimezone(timezone.utc).strftime(_DATETIME_Z_FORMAT)


def _parse_datetime_z(value: str) -> datetime:
    return datetime.strptime(value, _DATETIME_Z_FORMAT).replace(tzinfo=timezone.utc)


def build_frontmatter(
    item: dict,
    artefact: dict | None = None,
    labels: dict | None = None,
    label_provenance: list | None = None,
) -> dict:
    """Builds the schema-1 frontmatter dict for `item`.

    `item` must carry: id, regulator_id, regulator_name, jurisdiction,
    title, url, published_at, fetched_at, updated_at, date_inferred,
    language, source_label, source_type, body_md. `labels` is whatever
    items.labels currently holds (populated by a parallel task) - an empty
    or missing value is rendered as no `labels` key at all, never an error.
    """
    published_at = _as_date(item.get("published_at")) or _as_date(item["fetched_at"])
    fetched_at = _as_utc_datetime(item["fetched_at"])
    updated_at = _as_utc_datetime(item.get("updated_at") or item["fetched_at"])

    fm: dict = {
        "regradar_schema": SCHEMA_VERSION,
        "id": item["id"],
        "regulator": item["regulator_id"],
        "regulator_name": item.get("regulator_name") or item["regulator_id"],
        "jurisdiction": (item.get("jurisdiction") or "unknown").lower(),
        "jurisdiction_ancestors": item.get("jurisdiction_ancestors") or [],
        "title": item.get("title") or "",
        "published_at": published_at,
        "fetched_at": fetched_at,
        "updated_at": updated_at,
        "date_inferred": bool(item.get("date_inferred")),
        "language": item.get("language") or "",
        "source_url": item.get("url") or "",
        "source_label": item.get("source_label") or "",
        "source_type": item.get("source_type") or "",
    }
    if labels:
        fm["labels"] = labels
    if label_provenance:
        fm["label_provenance"] = label_provenance
    fm["content_hash"] = _content_hash(item.get("body_md") or "")
    if artefact:
        fm["original"] = {
            "path": artefact["path"],
            "mime": artefact["mime"],
            "sha256": artefact["sha256"],
            "bytes": artefact["bytes"],
        }
    return fm


def render_item(item: dict, artefact: dict | None, labels: dict | None, provenance: list | None) -> str:
    """Renders `item` to the full canonical document: YAML frontmatter,
    then the Markdown body (item["body_md"]).
    """
    fm = build_frontmatter(item, artefact=artefact, labels=labels, label_provenance=provenance)
    fm_for_yaml = dict(fm)
    for key in ("fetched_at", "updated_at"):
        fm_for_yaml[key] = _format_datetime_z(fm[key])
    yaml_text = yaml.safe_dump(fm_for_yaml, sort_keys=False, allow_unicode=True, default_flow_style=False).strip()
    body = (item.get("body_md") or "").strip()
    return f"---\n{yaml_text}\n---\n\n{body}\n"


def parse(text: str) -> tuple[dict, str]:
    """Inverse of render_item(): returns (frontmatter_dict, body). Restores
    fetched_at/updated_at to timezone-aware UTC datetimes (they're written
    as Z-suffixed strings, not left to YAML's own timestamp resolution) so
    the round trip matches build_frontmatter()'s types field-for-field.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise ValueError("not a valid canonical document: missing frontmatter block")
    fm = yaml.safe_load(match.group(1)) or {}
    for key in ("fetched_at", "updated_at"):
        if key in fm and isinstance(fm[key], str):
            fm[key] = _parse_datetime_z(fm[key])
    body = match.group(2)
    return fm, body


def frontmatter_to_json_safe(fm: dict) -> dict:
    """Frontmatter with date/datetime values converted to ISO strings, for
    JSON Schema validation (jsonschema works on JSON-native types only).
    fetched_at/updated_at use the same Z-suffixed form render_item() writes
    (docs/schema/document-v1.json's pattern expects exactly that).
    """
    out = dict(fm)
    for key in ("fetched_at", "updated_at"):
        value = out.get(key)
        if isinstance(value, datetime):
            out[key] = _format_datetime_z(value)
    value = out.get("published_at")
    if isinstance(value, date):
        out["published_at"] = value.isoformat()
    return out

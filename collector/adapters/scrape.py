"""CSS-selector scrape adapter - wraps v1's `_parse_scrape` unchanged."""
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from collector.adapters.base import FetchContext, RawDocument, SourceAdapter
from collector.adapters.http import (
    DEFAULT_USER_AGENT,
    MAX_PDF_DOWNLOAD_BYTES,
    extract_pdf_text,
    fetch_bytes,
    fetch_html,
)

logger = logging.getLogger(__name__)


class ScrapeAdapter(SourceAdapter):
    type_id = "scrape"

    def fetch(self, source_cfg: dict, ctx: FetchContext) -> list[RawDocument]:
        html = self.fetch_raw(source_cfg, ctx)
        return self.parse(html, source_cfg, ctx.regulator_id, {
            "user_agent": ctx.user_agent, "timeout": ctx.timeout, "retries": ctx.retries,
        })

    def fetch_raw(self, source_cfg: dict, ctx: FetchContext) -> str:
        expected_selector = source_cfg.get("scrape", {}).get("item_selector")
        html = fetch_html(
            source_cfg["url"], ctx.user_agent, ctx.timeout, ctx.retries,
            ctx.flaresolverr_url, expected_selector,
        )
        if html is None:
            raise RuntimeError(f"Failed to fetch {source_cfg['url']} for {ctx.regulator_id}/{source_cfg['label']}")
        return html

    def parse(self, html: str, source_cfg: dict, regulator_id: str, config: dict | None = None) -> list[RawDocument]:
        scrape_cfg = source_cfg.get("scrape", {})
        item_selector = scrape_cfg.get("item_selector")
        title_selector = scrape_cfg.get("title_selector")
        link_selector = scrape_cfg.get("link_selector")
        date_selector = scrape_cfg.get("date_selector")
        date_attribute = scrape_cfg.get("date_attribute")
        content_selector = scrape_cfg.get("content_selector")
        extract_pdf = scrape_cfg.get("extract_pdf_text", False)
        date_fuzzy = scrape_cfg.get("date_fuzzy", False)
        config = config or {}

        if not item_selector or not title_selector or not link_selector:
            logger.error("Scrape source %s/%s missing required selectors", regulator_id, source_cfg["label"])
            return []

        soup = BeautifulSoup(html, "lxml")
        docs = []

        for element in soup.select(item_selector):
            title_el = element.select_one(title_selector)
            link_el = element.select_one(link_selector)
            if not title_el or not link_el:
                continue

            title = title_el.get_text(strip=True)
            href = link_el.get("href", "").strip()
            if not href:
                continue
            if not href.startswith("http"):
                href = urljoin(source_cfg["url"], href)

            published_at = None
            date_inferred = True
            if date_selector:
                date_el = element.select_one(date_selector)
                if date_el:
                    date_str = date_el.get(date_attribute) if date_attribute else date_el.get_text(strip=True)
                    if date_str:
                        parsed = _parse_date(date_str, date_fuzzy, date_attribute)
                        if parsed is not None:
                            published_at = parsed
                            date_inferred = False

            content_html = None
            pdf_text = None
            pdf_bytes = None
            if extract_pdf:
                # The item's own link is the document itself (e.g. CNMV's
                # warning notices link straight to a PDF, no separate HTML
                # landing page).
                pdf_bytes = fetch_bytes(
                    href,
                    config.get("user_agent", DEFAULT_USER_AGENT),
                    int(config.get("timeout", 30)),
                    int(config.get("retries", 3)),
                    max_bytes=MAX_PDF_DOWNLOAD_BYTES,
                    # A blocked attachment must not discard the listing
                    # items already parsed above - the listing fetch is what
                    # decides whether this source is blocked (#39).
                    raise_on_block=False,
                )
                if pdf_bytes:
                    pdf_text = extract_pdf_text(pdf_bytes) or None
            if not pdf_text and content_selector:
                content_el = element.select_one(content_selector)
                if content_el:
                    content_html = str(content_el)

            body = pdf_bytes if pdf_bytes else (content_html.encode("utf-8") if content_html else b"")
            mime = "application/pdf" if pdf_bytes else ("text/html" if content_html else "")

            docs.append(RawDocument(
                url=href,
                title=title,
                published_at=published_at,
                date_inferred=date_inferred,
                body=body,
                mime=mime,
                pdf_text=pdf_text,
                content_html=content_html,
            ))
        return docs


_ISO_DATE = re.compile(r"\s*\d{4}-\d{1,2}-\d{1,2}")


def _parse_date(date_str: str, fuzzy: bool, date_attribute: str | None) -> datetime | None:
    try:
        from dateutil.parser import parse as parse_date
        # dayfirst: EEA/UK sources render free-text dates as DD/MM/YYYY -
        # without this, ambiguous dates like 02/07/2026 silently misparse as
        # 2 July -> 7 Feb. Only applied when reading free text (no
        # date_attribute); an unambiguous ISO datetime="..." attribute is
        # NOT left alone by dateutil's dayfirst=True either (verified
        # directly), so this stays keyed off date_attribute, matching v1.
        #
        # fuzzy is NOT a safe global default - it silently extracts a
        # date-shaped substring from surrounding text and never fails
        # loudly. Opt in per-source via date_fuzzy: true only for sources
        # that genuinely need it (e.g. FCA's "Published: DD/MM/YYYY" text).
        #
        # An ISO-shaped string read as TEXT is unambiguous, and dayfirst=True
        # actively corrupts it: dateutil reads "2026-08-03" as 8 March and
        # "2026-12-01" as 12 January, stored with date_inferred=False - i.e.
        # asserted as fact. NYDFS's tables are the first source to render
        # dates that way (no <time datetime>, bare text), but the fix is keyed
        # off the string's shape rather than a per-source flag, so every
        # ISO-text source is right by default.
        iso_text = _ISO_DATE.match(date_str) is not None
        parsed = parse_date(date_str, fuzzy=fuzzy, dayfirst=not date_attribute and not iso_text)
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None

"""Acceptance: parse(render(item)) == item for all frontmatter fields, on
at least 20 items spanning rss, scrape and page_watch sources (no network -
items are constructed the same way collector.fetcher would produce them,
then run through the real insert_item -> DB -> render.py load path).
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jsonschema

from collector.schema import Item, make_item_id, make_page_watch_item_id
from collector.storage import init_db, insert_item, upsert_regulator
from mcpserver.queries import _document_fields
from regradar import document as doc
from regradar.render import _load_item

SCHEMA = json.loads((Path(__file__).parent.parent / "regradar" / "schema" / "document-v1.json").read_text())

REGULATORS = [
    ("fca", "Financial Conduct Authority", "GB"),
    ("de-bafin", "Bundesanstalt für Finanzdienstleistungsaufsicht", "DE"),
    ("esma", "European Securities and Markets Authority", "EU"),
    ("es-cnmv", "Comisión Nacional del Mercado de Valores", "ES"),
]

TITLES = [
    "MiCA guidance on crypto-asset marketing",
    "CP26/12: Proposed changes to the client assets sourcebook",
    "Warnung vor unerlaubten Anlagegeschäften",
    "Advertencia sobre entidades no autorizadas",
    "Consultation on technical standards",
    "Ανακοίνωση εποπτείας",  # Greek, folds to "untitled"
]


def _build_items() -> list[Item]:
    items = []
    now = datetime.now(timezone.utc)
    for i in range(20):
        regulator_id, _name, _jur = REGULATORS[i % len(REGULATORS)]
        title = TITLES[i % len(TITLES)]
        source_type = ["rss", "scrape", "page_watch"][i % 3]
        published_at = None if i % 5 == 0 else now - timedelta(days=i)
        date_inferred = published_at is None
        url = f"https://example.org/{regulator_id}/item-{i}"

        if source_type == "page_watch":
            item_id = make_page_watch_item_id(regulator_id, url, f"hash{i}")
        else:
            item_id = make_item_id(regulator_id, url, published_at)

        raw_bytes = None
        mime = None
        if source_type == "scrape" and i % 2 == 0:
            raw_bytes = f"<p>content for item {i}</p>".encode()
            mime = "text/html"

        items.append(Item(
            id=item_id,
            regulator_id=regulator_id,
            source_label="all_news",
            title=title,
            url=url,
            published_at=published_at,
            fetched_at=now,
            date_inferred=date_inferred,
            language="en",
            content_snippet=f"snippet {i}",
            source_type=source_type,
            source_tags=["casp"],
            content_tags=[],
            raw_content=f"content for item {i}",
            body_md=f"content for item {i}",
            extractor="raw/1",
            raw_bytes=raw_bytes,
            mime=mime,
        ))
    return items


def test_twenty_items_round_trip_losslessly(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path / "originals"))
    engine = init_db(str(tmp_path / "test.db"))
    for regulator_id, name, jurisdiction in REGULATORS:
        upsert_regulator(engine, {
            "id": regulator_id, "name": name, "full_name": name, "jurisdiction": jurisdiction,
            "jurisdiction_type": "national", "website": "https://example.org",
            "mica_competent_authority": True, "enabled": True,
        })

    items = _build_items()
    assert len(items) >= 20
    for item in items:
        assert insert_item(engine, item)

    source_types_seen = set()
    for item in items:
        loaded, reg = _load_item(engine, item.id)
        assert loaded is not None
        source_types_seen.add(loaded["source_type"])
        from collector.storage import get_artefact
        artefact = get_artefact(engine, item.id)
        doc_item, labels, label_provenance = _document_fields(loaded, reg, None, [])

        fm_expected = doc.build_frontmatter(doc_item, artefact=artefact, labels=labels, label_provenance=label_provenance)
        text = doc.render_item(doc_item, artefact, labels, label_provenance)
        fm_actual, body = doc.parse(text)

        assert fm_actual == fm_expected, item.id
        jsonschema.validate(doc.frontmatter_to_json_safe(fm_actual), SCHEMA)

    assert source_types_seen == {"rss", "scrape", "page_watch"}

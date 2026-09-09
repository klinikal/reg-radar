import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from urllib.parse import urlencode

from bs4 import BeautifulSoup
from fastapi import Body, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from datetime import datetime, timezone
from xml.etree import ElementTree as ET
from fastapi.responses import Response

from collector.storage import init_db
from mcpserver.queries import get_item, get_month_facets, get_unclassified, list_regulators, search_items_advanced
from regradar.classification import remove_classification, write_classification
from regradar.profiles import ProfileError, get_expanded_profile, list_shipped_profiles, load_activities
from regradar.rules import load_raw_rules, write_raw_rules
from regradar.taxonomy import load_and_exit_on_error
from mcpserver.queries import get_changes, get_latest_changes
from regradar.changes import CursorError

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

_HERE = os.path.dirname(__file__)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Loads taxonomy and activities once at startup so a broken
    config/taxonomy.yaml, jurisdictions.yaml or activities.yaml kills the
    process at boot. Loading them lazily inside a request handler meant
    load_and_exit_on_error()'s sys.exit surfaced as a per-request 500 on a
    server that stayed up. Request handlers keep calling get_taxonomy()/
    get_activities(), which now only ever return the already-loaded
    objects (and still load on demand when the lifespan never ran - a
    TestClient not used as a context manager, which is how the tests
    inject their own config paths)."""
    get_taxonomy()
    get_activities()
    yield


app = FastAPI(title="regradar", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(_HERE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(_HERE, "templates"))

PAGE_SIZE = 50
RULES_PATH = os.getenv("RULES_PATH", "/app/config/rules.yaml")
FEED_MAX_ITEMS = int(os.getenv("FEED_MAX_ITEMS", "50"))
_ATOM_NS = "http://www.w3.org/2005/Atom"

_engine = None
_taxonomy = None
_activities = None
PROFILES_DIR = os.getenv("PROFILES_DIR", "/app/config/profiles")


def get_engine():
    global _engine
    if _engine is None:
        db_path = os.getenv("DB_PATH", "/app/data/regradar.db")
        _engine = init_db(db_path)
    return _engine


def get_taxonomy():
    global _taxonomy
    if _taxonomy is None:
        _taxonomy = load_and_exit_on_error(
            os.getenv("TAXONOMY_PATH", "/app/config/taxonomy.yaml"),
            os.getenv("JURISDICTIONS_PATH", "/app/config/jurisdictions.yaml"),
        )
    return _taxonomy


def get_activities():
    global _activities
    if _activities is None:
        _activities = load_activities(os.getenv("ACTIVITIES_PATH", "/app/config/activities.yaml"), get_taxonomy())
    return _activities


def _resolve_profile(profile: str):
    """Turns the `profile` query param into a regradar.profiles.ExpandedProfile,
    or None if not set. Unlike the MCP tools/CLI, `profile` here is
    unauthenticated user input over HTTP, and
    regradar.profiles.load_profile treats any name that isn't a shipped
    template as a literal filesystem path to open - so the web surface
    only ever accepts a name from list_shipped_profiles(), never passing
    arbitrary input through to load_profile. Anything else (an absolute
    path, "../" traversal, or just an unknown name) gets a generic 400
    that never echoes the input or any file content."""
    if not profile:
        return None
    if profile not in list_shipped_profiles(PROFILES_DIR):
        raise HTTPException(status_code=400, detail="unknown profile")
    try:
        return get_expanded_profile(profile, get_taxonomy(), get_activities(), profiles_dir=PROFILES_DIR)
    except ProfileError:
        raise HTTPException(status_code=400, detail="unknown profile")


def _regulator_ids_for_jurisdictions(jurisdictions: list[str], regulators: list[dict]) -> list[str] | None:
    """Expands each selected jurisdiction through ancestors() (architecture
    doc §4.2 - a filter on "eu" also matches "fr", "de", ...) and returns
    the matching regulator ids, or None if no jurisdiction filter is set."""
    if not jurisdictions:
        return None
    taxonomy = get_taxonomy()
    wanted = set()
    for j in jurisdictions:
        j = j.lower()
        wanted.add(j)
        wanted.update(taxonomy.ancestors(j))
    return [r["id"] for r in regulators if (r.get("jurisdiction") or "").lower() in wanted]


def _clean_text(raw_content: str | None, fallback: str | None) -> str:
    # raw_content is scraped third-party HTML - never render it as HTML.
    # Strip to plain text here; Jinja2 autoescapes on top as a second layer.
    if raw_content:
        return BeautifulSoup(raw_content, "lxml").get_text(separator="\n\n").strip()
    return fallback or ""


def _parse_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return []


def _with_tags(item: dict) -> dict:
    item["source_tag_list"] = _parse_tags(item.get("source_tags"))
    item["content_tag_list"] = _parse_tags(item.get("content_tags"))
    return item


def _with_search_fields(regulator: dict) -> dict:
    # Country name comes from config/jurisdictions.yaml, the single source of
    # jurisdiction ids and names - a second hand-maintained map here went
    # stale the moment an id changed (UK -> gb), losing the country name from
    # the regulator panel and breaking search-by-country for it.
    country = get_taxonomy().jurisdiction_name((regulator["jurisdiction"] or "").lower())
    regulator["country"] = country
    regulator["display_label"] = f"{country} - {regulator['name']} - {regulator['full_name']}"
    regulator["search_text"] = " ".join([
        country, regulator["jurisdiction"], regulator["name"], regulator["full_name"], regulator["id"],
    ]).lower()
    return regulator


def _search(q, exact, regulator_ids, date_from, date_to, page, profile=None):
    offset = (page - 1) * PAGE_SIZE
    items, total = search_items_advanced(
        get_engine(),
        query=q,
        exact=exact,
        regulator_ids=regulator_ids or None,
        date_from=date_from or None,
        date_to=date_to or None,
        profile=profile,
        limit=PAGE_SIZE,
        offset=offset,
    )
    return items, total


def _month_bounds(month: str) -> tuple[str, str]:
    year, mon = (int(p) for p in month.split("-"))
    last_day = calendar.monthrange(year, mon)[1]
    return f"{month}-01", f"{month}-{last_day:02d}"


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    q: str = "",
    exact: bool = False,
    regulator_id: list[str] = Query([]),
    jurisdiction: list[str] = Query([]),
    date_from: str = "",
    date_to: str = "",
    profile: str = "",
    page: int = Query(1, ge=1),
):
    regulators = [_with_search_fields(r) for r in list_regulators(get_engine())]
    regulators.sort(key=lambda r: (r["country"], r["name"]))
    regulator_names = {r["id"]: r["name"] for r in regulators}

    jurisdiction_regulator_ids = _regulator_ids_for_jurisdictions(jurisdiction, regulators)
    effective_regulator_ids = regulator_id
    if jurisdiction_regulator_ids is not None:
        effective_regulator_ids = (
            [r for r in regulator_id if r in jurisdiction_regulator_ids] if regulator_id
            else jurisdiction_regulator_ids
        )

    expanded_profile = _resolve_profile(profile)
    items, total = _search(q, exact, effective_regulator_ids, date_from, date_to, page, profile=expanded_profile)
    items = [_with_tags(i) for i in items]
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    months = get_month_facets(get_engine(), query=q, exact=exact, regulator_ids=effective_regulator_ids or None,
                               profile=expanded_profile)
    for m in months:
        m["date_from"], m["date_to"] = _month_bounds(m["month"])
        m["active"] = date_from == m["date_from"] and date_to == m["date_to"]

    # Jurisdiction filter panel options: only jurisdictions actually in use
    # by a configured regulator, so the panel doesn't list ids nobody has.
    taxonomy = get_taxonomy()
    used_jurisdictions = sorted({(r.get("jurisdiction") or "").lower() for r in regulators} - {""})
    jurisdiction_options = [{"id": j, "name": taxonomy.jurisdiction_name(j)} for j in used_jurisdictions]

    profile_options = list_shipped_profiles(PROFILES_DIR)

    def qs(**overrides):
        base = {"q": q, "exact": "true" if exact else "", "regulator_id": regulator_id,
                "jurisdiction": jurisdiction, "date_from": date_from, "date_to": date_to,
                "profile": profile}
        base.update(overrides)
        return urlencode({k: v for k, v in base.items() if v}, doseq=True)

    filter_qs = qs()
    filter_qs_no_date = qs(date_from="", date_to="")
    filter_qs_no_regulator = qs(regulator_id=[])
    filter_qs_no_jurisdiction = qs(jurisdiction=[])

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "items": items,
            "regulators": regulators,
            "regulator_names": regulator_names,
            "jurisdiction_options": jurisdiction_options,
            "profile_options": profile_options,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "page_size": PAGE_SIZE,
            "filter_qs": filter_qs,
            "filter_qs_no_date": filter_qs_no_date,
            "filter_qs_no_regulator": filter_qs_no_regulator,
            "filter_qs_no_jurisdiction": filter_qs_no_jurisdiction,
            "months": months,
            "q": q,
            "exact": exact,
            "regulator_id": regulator_id,
            "jurisdiction": jurisdiction,
            "date_from": date_from,
            "date_to": date_to,
            "profile": profile,
        },
    )


def _parse_labels(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


@app.get("/item/{item_id}", response_class=HTMLResponse)
def item_view(request: Request, item_id: str):
    taxonomy = get_taxonomy()
    item = get_item(get_engine(), item_id, taxonomy=taxonomy)
    if item is None:
        return templates.TemplateResponse(
            request, "not_found.html", {"item_id": item_id}, status_code=404
        )
    item["has_full_text"] = bool(item.get("raw_content"))
    item["clean_text"] = _clean_text(item.get("raw_content"), item.get("content_snippet"))
    item = _with_tags(item)
    item_labels = _parse_labels(item.get("labels"))
    regulators = {r["id"]: r["name"] for r in list_regulators(get_engine())}
    document_type_values = [v.id for v in taxonomy.axis_values("document_type")]
    instrument_values = [v.id for v in taxonomy.axis_values("instrument")]
    return templates.TemplateResponse(
        request,
        "item.html",
        {
            "item": item,
            "regulator_name": regulators.get(item["regulator_id"]),
            "item_labels": item_labels,
            "document_type_values": document_type_values,
            "instrument_values": instrument_values,
            "current_document_type": item_labels.get("document_type"),
            "current_instruments": item_labels.get("instrument", []),
        },
    )


@app.post("/item/{item_id}/classify")
def item_classify(item_id: str, axis: str = Form(...), value: str = Form(...), action: str = Form("set")):
    # ponytail: no auth/identity system in this single-instance tool - "who"
    # made a web UI correction is a fixed provenance string, not a real name.
    by = "human:web-ui"
    taxonomy = get_taxonomy()
    if action == "remove":
        remove_classification(get_engine(), taxonomy, item_id, axis, value, by)
    else:
        try:
            write_classification(get_engine(), taxonomy, item_id, axis, value, by=by, confidence=1.0)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    return RedirectResponse(f"/item/{item_id}", status_code=303)


@app.get("/alerts")
def alerts_view():
    # Renamed to /rules and rebuilt on config/rules.yaml (architecture doc
    # §4.3) - this issue owns the migration; see scripts/migrate_alerts_to_rules.py.
    return RedirectResponse("/rules", status_code=308)


@app.get("/rules", response_class=HTMLResponse)
def rules_view(request: Request):
    taxonomy = get_taxonomy()
    min_confidence, rules = load_raw_rules(RULES_PATH)
    axis_values = {axis: [v.id for v in taxonomy.axis_values(axis)] for axis in taxonomy.axes()}
    return templates.TemplateResponse(
        request,
        "rules.html",
        {
            "rules": rules, "min_confidence": min_confidence,
            "axes": taxonomy.axes(), "axis_values": axis_values,
        },
    )


@app.post("/rules")
def rules_add(axis: str = Form(...), value: str = Form(...), pattern: str = Form(...),
              weight: float = Form(0.7), flags: str = Form(""), fields: list[str] = Form(["title", "body"]),
              title_multiplier: float = Form(1.0)):
    taxonomy = get_taxonomy()
    pattern = pattern.strip()
    if axis not in taxonomy.axes() or not taxonomy.is_valid(axis, value):
        raise HTTPException(status_code=400, detail="unknown axis/value - see config/taxonomy.yaml")
    if not pattern:
        raise HTTPException(status_code=400, detail="pattern is required")
    try:
        re.compile(pattern)
    except re.error as e:
        raise HTTPException(status_code=400, detail=f"invalid regex pattern: {e}")
    match_entry = {"pattern": pattern, "weight": weight}
    if flags.strip():
        match_entry["flags"] = flags.strip()
    min_confidence, rules = load_raw_rules(RULES_PATH)
    rules.append({
        "axis": axis, "value": value, "match": [match_entry],
        "fields": fields or ["title", "body"], "title_multiplier": title_multiplier,
    })
    write_raw_rules(RULES_PATH, min_confidence, rules)
    return RedirectResponse("/rules", status_code=303)


@app.post("/rules/{index}/delete")
def rules_delete(index: int):
    min_confidence, rules = load_raw_rules(RULES_PATH)
    if 0 <= index < len(rules):
        rules.pop(index)
        write_raw_rules(RULES_PATH, min_confidence, rules)
    return RedirectResponse("/rules", status_code=303)


@app.get("/api/unclassified")
def api_unclassified(axis: str, limit: int = 50, offset: int = 0, regulator_id: str = "", since: str = "",
                      include_low_confidence: bool = False):
    items = get_unclassified(get_engine(), axis=axis, limit=limit, offset=offset,
                              regulator_id=regulator_id or None, since=since or None,
                              include_low_confidence=include_low_confidence)
    return {"items": items}


# JSON API - same query layer as the HTML views, so a future non-HTML
# frontend (or a script) can use this without duplicating query logic.
@app.get("/api/items")
def api_items(
    q: str = "",
    exact: bool = False,
    regulator_id: list[str] = Query([]),
    date_from: str = "",
    date_to: str = "",
    profile: str = "",
    page: int = Query(1, ge=1),
):
    items, total = _search(q, exact, regulator_id, date_from, date_to, page, profile=_resolve_profile(profile))
    return {"items": items, "total": total, "page": page, "page_size": PAGE_SIZE}


@app.get("/api/months")
def api_months(q: str = "", exact: bool = False, regulator_id: list[str] = Query([]), profile: str = ""):
    return {"months": get_month_facets(get_engine(), query=q, exact=exact, regulator_ids=regulator_id or None,
                                        profile=_resolve_profile(profile))}


@app.get("/api/items/{item_id}")
def api_item(item_id: str):
    item = get_item(get_engine(), item_id, taxonomy=get_taxonomy())
    if item is None:
        raise HTTPException(status_code=404, detail="not found")
    return item


@app.get("/api/regulators")
def api_regulators():
    return {"regulators": list_regulators(get_engine())}


@app.post("/api/items/{item_id}/classifications")
def api_classify_item(item_id: str, body: dict = Body(...)):
    """Human correction endpoint (architecture doc §4.5): writes through
    the same write_classification() the collector and migrations use.

    Body: {"axis": str, "value": str, "by": str, "confidence"?: float,
    "classifier_id"?: str, "remove"?: bool}. `by` should be `human:<name>`
    for a genuine human correction (any string is accepted - validation is
    against the taxonomy axis/value, not the provenance string). `remove:
    true` supersedes that exact live row instead of writing a new one (used
    to retract one instrument from a multi-valued axis).
    """
    axis = body.get("axis")
    value = body.get("value")
    by = body.get("by")
    if not axis or not value or not by:
        raise HTTPException(status_code=400, detail="axis, value and by are all required")

    taxonomy = get_taxonomy()
    if body.get("remove"):
        remove_classification(get_engine(), taxonomy, item_id, axis, value, by)
    else:
        try:
            write_classification(
                get_engine(), taxonomy, item_id, axis, value, by=by,
                confidence=float(body.get("confidence", 1.0)), classifier_id=body.get("classifier_id"),
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    item = get_item(get_engine(), item_id)
    return {"item_id": item_id, "labels": _parse_labels(item.get("labels")) if item else {}}


# Export and sync (architecture doc section 6) - the change-stream
# primitive and its Atom feed adapter, same query layer as everything
# else. /api/changes mirrors get_changes_tool; /feeds/<profile>.atom
# reuses get_latest_changes(), which orders newest-first (get_changes()
# itself is a forward cursor page, not a "latest N" query).
@app.get("/api/changes")
def api_changes(since: str = "", profile: str = "", limit: int = 100, include_body: bool = False):
    try:
        items, next_cursor = get_changes(
            get_engine(), since=since or None, profile=_resolve_profile(profile),
            limit=limit, include_body=include_body, taxonomy=get_taxonomy(),
        )
    except CursorError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"items": items, "next_cursor": next_cursor}


def _now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atom_summary(doc: dict) -> str:
    # Summary content only, linking to the item - never the full body
    # (architecture doc section 6: outbound feeds are a discovery
    # mechanism, not a bulk-export channel).
    regulator = doc.get("regulator_name") or doc.get("regulator") or ""
    published = doc.get("published_at") or ""
    return f"{regulator} - {published}. See source_url for the full item."


def _atom_feed_xml(profile_name: str, items: list[dict], feed_url: str) -> bytes:
    ET.register_namespace("", _ATOM_NS)
    feed = ET.Element(f"{{{_ATOM_NS}}}feed")
    ET.SubElement(feed, f"{{{_ATOM_NS}}}title").text = f"regradar - {profile_name}"
    ET.SubElement(feed, f"{{{_ATOM_NS}}}id").text = feed_url
    link = ET.SubElement(feed, f"{{{_ATOM_NS}}}link")
    link.set("href", feed_url)
    ET.SubElement(feed, f"{{{_ATOM_NS}}}updated").text = items[0]["updated_at"] if items else _now_z()
    author = ET.SubElement(feed, f"{{{_ATOM_NS}}}author")
    ET.SubElement(author, f"{{{_ATOM_NS}}}name").text = "regradar"

    for doc in items:
        entry = ET.SubElement(feed, f"{{{_ATOM_NS}}}entry")
        ET.SubElement(entry, f"{{{_ATOM_NS}}}title").text = doc.get("title") or "(untitled)"
        ET.SubElement(entry, f"{{{_ATOM_NS}}}id").text = f"regradar:item:{doc['id']}"
        entry_link = ET.SubElement(entry, f"{{{_ATOM_NS}}}link")
        entry_link.set("href", doc.get("source_url") or "")
        ET.SubElement(entry, f"{{{_ATOM_NS}}}updated").text = doc["updated_at"]
        ET.SubElement(entry, f"{{{_ATOM_NS}}}summary").text = _atom_summary(doc)

    return ET.tostring(feed, encoding="utf-8", xml_declaration=True)


@app.get("/feeds/{profile_name}.atom")
def feed_atom(request: Request, profile_name: str):
    expanded = _resolve_profile(profile_name)
    items = get_latest_changes(get_engine(), profile=expanded, limit=FEED_MAX_ITEMS, taxonomy=get_taxonomy())
    xml_bytes = _atom_feed_xml(profile_name, items, str(request.url))
    return Response(content=xml_bytes, media_type="application/atom+xml")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)

import calendar
import json
import logging
import os
from urllib.parse import urlencode

from bs4 import BeautifulSoup
from fastapi import Body, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from collector.keyword_alerts import load_alert_rules, write_alert_rules
from collector.storage import init_db
from mcpserver.queries import get_item, get_month_facets, list_regulators, search_items_advanced
from regradar.classification import remove_classification, write_classification
from regradar.profiles import ProfileError, get_expanded_profile, list_shipped_profiles, load_activities
from regradar.taxonomy import load_and_exit_on_error

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

_HERE = os.path.dirname(__file__)

app = FastAPI(title="regradar")
app.mount("/static", StaticFiles(directory=os.path.join(_HERE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(_HERE, "templates"))

PAGE_SIZE = 50
ALERTS_PATH = os.getenv("ALERTS_PATH", "/app/config/alerts.yaml")

# Full country/entity names for jurisdiction codes used in config/regulators.yaml,
# so the regulator filter can be searched by country name ("czech"), not just
# abbreviation (CNB) or code (CZ). Extend this if a new jurisdiction is added.
JURISDICTION_NAMES = {
    "AT": "Austria", "BE": "Belgium", "BG": "Bulgaria", "CY": "Cyprus",
    "CZ": "Czech Republic", "DE": "Germany", "DK": "Denmark", "EE": "Estonia",
    "ES": "Spain", "EU": "European Union", "FI": "Finland", "FR": "France",
    "GR": "Greece", "HR": "Croatia", "HU": "Hungary", "IE": "Ireland",
    "IS": "Iceland", "IT": "Italy", "LT": "Lithuania", "LU": "Luxembourg",
    "LV": "Latvia", "MT": "Malta", "NL": "Netherlands", "NO": "Norway",
    "PL": "Poland", "PT": "Portugal", "RO": "Romania", "SE": "Sweden",
    "SI": "Slovenia", "SK": "Slovakia", "UK": "United Kingdom",
}

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
    """Turns the `profile` query param (a shipped template name or a path)
    into a regradar.profiles.ExpandedProfile, or None if not set. Raises
    HTTPException(400) on an unknown profile rather than a 500, since this
    is user-supplied query-string input."""
    if not profile:
        return None
    try:
        return get_expanded_profile(profile, get_taxonomy(), get_activities(), profiles_dir=PROFILES_DIR)
    except ProfileError as e:
        raise HTTPException(status_code=400, detail=str(e))


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
    country = JURISDICTION_NAMES.get(regulator["jurisdiction"], regulator["jurisdiction"])
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
        "index.html",
        {
            "request": request,
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
            "not_found.html", {"request": request, "item_id": item_id}, status_code=404
        )
    item["has_full_text"] = bool(item.get("raw_content"))
    item["clean_text"] = _clean_text(item.get("raw_content"), item.get("content_snippet"))
    item = _with_tags(item)
    item_labels = _parse_labels(item.get("labels"))
    regulators = {r["id"]: r["name"] for r in list_regulators(get_engine())}
    document_type_values = [v.id for v in taxonomy.axis_values("document_type")]
    instrument_values = [v.id for v in taxonomy.axis_values("instrument")]
    return templates.TemplateResponse(
        "item.html",
        {
            "request": request,
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


@app.get("/alerts", response_class=HTMLResponse)
def alerts_view(request: Request):
    rules = load_alert_rules(ALERTS_PATH)
    return templates.TemplateResponse("alerts.html", {"request": request, "rules": rules})


@app.post("/alerts")
def alerts_add(keyword: str = Form(...), tag: str = Form(...)):
    keyword = keyword.strip()
    tag = tag.strip()
    if not keyword or not tag:
        raise HTTPException(status_code=400, detail="keyword and tag are both required")
    rules = load_alert_rules(ALERTS_PATH)
    rules.append({"keyword": keyword, "tag": tag})
    write_alert_rules(ALERTS_PATH, rules)
    return RedirectResponse("/alerts", status_code=303)


@app.post("/alerts/{index}/delete")
def alerts_delete(index: int):
    rules = load_alert_rules(ALERTS_PATH)
    if 0 <= index < len(rules):
        rules.pop(index)
        write_alert_rules(ALERTS_PATH, rules)
    return RedirectResponse("/alerts", status_code=303)


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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)

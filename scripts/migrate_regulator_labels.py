"""One-off migration: renames every source's `tags` (the old flat,
blanket-applied structural list) to `labels` (architecture doc §4.3), and
lower-cases `jurisdiction` on both the regulators and their DB rows (`UK`
becomes `gb` specifically - everything else is a straight lower-case).

Rewrites the given --regulators-path file in place using ruamel.yaml's
round-trip loader so comments and formatting survive - the `notes` fields
are the documentation of why sources are configured as they are, and this
is the one script in the repo allowed to depend on ruamel.yaml for that
reason (see collector/requirements.txt). Predates the config/regulators/
per-jurisdiction split - point it at one file at a time (e.g.
config/regulators/mt.yaml); it's a completed migration, already-migrated
files are a no-op.

Mapping decisions (architecture doc §4.7):
- `tags` containing "warnings"/"page_watch"/"press_releases"/"news" get a
  `document_type` source default, in that priority order when more than one
  is present on the same source (most specific wins). "publications" is too
  broad to map on its own and gets no document_type default.
- `tags` containing "mica"/"casp"/"aml"/"dora"/"art"/"emt"/"crypto" become an
  `instrument` source default ONLY on the small, explicit list of sources
  that are genuinely single-purpose (a dedicated MiCA hub page, a "crypto
  circulars" feed) - everywhere else those tags were stamped blanket-wide
  (see the issue's "Why": mica/casp sat on 108 of 114 tagged sources
  regardless of content) and are dropped, not migrated.

Every source ends up with a `labels` key (possibly `{}`) and no `tags` key.

Run from the repository root:

    .venv/bin/python -m scripts.migrate_regulator_labels --dry-run
    .venv/bin/python -m scripts.migrate_regulator_labels
"""
import argparse
import logging
import os

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from sqlalchemy import text

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

# Priority order (most specific first) when a source's old tags include
# more than one of these - only the first match becomes the document_type
# default.
_DOCUMENT_TYPE_PRIORITY = [
    ("warnings", "warning"),
    ("page_watch", "page_change"),
    ("press_releases", "press_release"),
    ("news", "press_release"),
]

# Explicit allowlist of (regulator_id, source_label) sources confirmed
# single-purpose by inspecting their label/url (not just their blanket
# tags) - see the task report for how this list was derived. Confidence
# for these labels is not decided here - it's read from each axis's
# source_default_confidence in taxonomy.yaml when the collector/reclassify
# actually write a classification (regradar/taxonomy.py).
_SINGLE_PURPOSE_INSTRUMENTS = {
    ("esma", "mica_hub_page"): ["mica"],
    ("mt-mfsa", "circulars_crypto_assets_circulars"): ["mica"],
    ("nl-dnb", "crypto_service_providers"): ["mica"],
}


def _map_jurisdiction(raw: str) -> str:
    return "gb" if raw == "UK" else raw.lower()


def decide_labels(regulator_id: str, source: dict) -> tuple[dict, str]:
    """Returns (labels, reason) for one source's old `tags` list."""
    tags = source.get("tags", []) or []
    labels: dict = {}
    reasons = []

    for tag, document_type in _DOCUMENT_TYPE_PRIORITY:
        if tag in tags:
            labels["document_type"] = document_type
            reasons.append(f"tag '{tag}' -> document_type={document_type}")
            break

    key = (regulator_id, source.get("label"))
    if key in _SINGLE_PURPOSE_INSTRUMENTS:
        labels["instrument"] = _SINGLE_PURPOSE_INSTRUMENTS[key]
        reasons.append(f"single-purpose source -> instrument={_SINGLE_PURPOSE_INSTRUMENTS[key]}")
    elif any(t in tags for t in ("mica", "casp", "aml", "dora", "art", "emt", "crypto")):
        reasons.append("instrument tag present but source is not single-purpose - dropped")

    if not reasons:
        reasons.append("no mapping - labels left empty")
    return labels, "; ".join(reasons)


def migrate_regulators(data: list) -> list:
    """Mutates `data` (a ruamel round-tripped structure) in place: renames
    tags->labels per decide_labels() and lower-cases jurisdiction. Returns
    the printable decision table as a list of row dicts.

    Idempotent: a source that already has `labels` and no `tags` (i.e. was
    migrated by a prior run) is left completely untouched and reported as
    "already migrated" - re-running must never recompute an empty `labels`
    from a `tags` key that no longer exists and silently wipe out a real
    decision from the first run."""
    table = []
    for reg in data:
        reg["jurisdiction"] = _map_jurisdiction(reg["jurisdiction"])
        for source in reg.get("sources", []):
            if "tags" not in source and "labels" in source:
                table.append({
                    "regulator_id": reg["id"], "source_label": source.get("label"),
                    "old_tags": [], "new_labels": dict(source["labels"]),
                    "reason": "already migrated - skipped",
                })
                continue

            old_tags = list(source.get("tags", []) or [])
            labels, reason = decide_labels(reg["id"], source)

            new_labels = CommentedMap()
            for k, v in labels.items():
                new_labels[k] = v

            if "tags" in source:
                idx = list(source.keys()).index("tags")
                source.insert(idx, "labels", new_labels)
                del source["tags"]
            else:
                source["labels"] = new_labels

            table.append({
                "regulator_id": reg["id"],
                "source_label": source.get("label"),
                "old_tags": old_tags,
                "new_labels": dict(labels),
                "reason": reason,
            })
    return table


def _has_any_tags(data: list) -> bool:
    return any("tags" in source for reg in data for source in reg.get("sources", []))


def _print_table(table: list) -> None:
    logger.info("%-12s %-45s %-35s %s", "regulator", "source", "old tags", "decision")
    for row in table:
        logger.info(
            "%-12s %-45s %-35s %s",
            row["regulator_id"], row["source_label"], row["old_tags"], row["reason"],
        )


def migrate_db_jurisdictions(db_path: str, dry_run: bool) -> int:
    """Lower-cases (and UK->gb maps) the `regulators` table's jurisdiction
    column to match the migrated YAML - architecture doc §4.7: "The
    regulators table jurisdiction column is rewritten in the same
    migration." A no-op if the DB doesn't exist yet (fresh/test environment
    - the collector's own upsert_regulator() would apply the same mapping
    on its next run regardless)."""
    if not os.path.exists(db_path):
        logger.info("No database at %s - skipping DB jurisdiction rewrite (collector will apply it on next run)", db_path)
        return 0
    from collector.storage import init_db

    engine = init_db(db_path)
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id, jurisdiction FROM regulators")).fetchall()
        changed = 0
        for reg_id, jurisdiction in rows:
            if jurisdiction is None:
                continue
            mapped = _map_jurisdiction(jurisdiction)
            if mapped != jurisdiction:
                changed += 1
                if not dry_run:
                    conn.execute(
                        text("UPDATE regulators SET jurisdiction = :j WHERE id = :id"),
                        {"j": mapped, "id": reg_id},
                    )
        if not dry_run:
            conn.commit()
    return changed


def run(regulators_path: str, db_path: str, dry_run: bool) -> None:
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096  # avoid ruamel re-wrapping long notes strings

    with open(regulators_path) as f:
        data = yaml.load(f)

    if not _has_any_tags(data):
        logger.info("%s has no `tags` keys left - already migrated, nothing to do", regulators_path)
        return

    table = migrate_regulators(data)
    _print_table(table)
    logger.info("%d source(s) processed%s", len(table), " [DRY RUN]" if dry_run else "")

    db_changed = migrate_db_jurisdictions(db_path, dry_run)
    logger.info("%d regulators table row(s) need a jurisdiction rewrite%s", db_changed, " [DRY RUN]" if dry_run else "")

    if not dry_run:
        with open(regulators_path, "w") as f:
            yaml.dump(data, f)
        logger.info("Wrote %s", regulators_path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Compute and print the decision table but write nothing")
    # Registry-split note: this migration predates config/regulators/ (one
    # file per jurisdiction) and only ever rewrites a single file in place -
    # no directory-aware default is offered here (silently guessing one
    # file out of 30+ would be wrong), so a stale REGULATORS_PATH env var
    # left over from before the split doesn't get picked up here.
    parser.add_argument(
        "--regulators-path", required=True,
        help="path to ONE regulators YAML file to migrate in place, e.g. config/regulators/mt.yaml "
             "(this is a completed, one-off migration - already-migrated files are a no-op, not an error)",
    )
    parser.add_argument("--db-path", default=os.getenv("DB_PATH", "data/regradar.db"))
    args = parser.parse_args()
    run(args.regulators_path, args.db_path, args.dry_run)


if __name__ == "__main__":
    main()

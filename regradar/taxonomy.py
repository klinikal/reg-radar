"""Loads and validates config/taxonomy.yaml and config/jurisdictions.yaml
(architecture doc §4.1, §4.2).

Code must not name any taxonomy axis except "jurisdiction" - axes and their
vocabularies come entirely from taxonomy.yaml; jurisdiction hierarchy comes
from jurisdictions.yaml. Anything that needs to iterate axes calls
Taxonomy.axes().
"""
import logging
import os
import sys
from dataclasses import dataclass, field

import yaml

logger = logging.getLogger(__name__)


class TaxonomyError(ValueError):
    """Raised when taxonomy.yaml or jurisdictions.yaml (or a regulator's
    reference into them) is structurally invalid."""


@dataclass
class AxisValue:
    id: str
    name: str
    aliases: list[str] = field(default_factory=list)
    jurisdiction: str | None = None


@dataclass
class Axis:
    id: str
    cardinality: str  # "one" or "many"
    values: dict  # value id -> AxisValue
    min_confidence: float = 0.5
    # Confidence a `by: source_default` write gets on this axis (architecture
    # doc §4.3: 1.0 for document_type on a single-purpose feed, 0.6 for
    # instrument - a feed named "crypto circulars" is strong evidence, not
    # proof). Config-driven so no code branches on an axis name.
    source_default_confidence: float = 0.6


class Taxonomy:
    def __init__(self, version: int, axes: dict, jurisdictions: dict):
        self._version = version
        self._axes = axes
        self._jurisdictions = jurisdictions

    def version(self) -> int:
        return self._version

    def axes(self) -> list[str]:
        return list(self._axes.keys())

    def cardinality(self, axis: str) -> str:
        return self._axes[axis].cardinality

    def min_confidence(self, axis: str) -> float:
        return self._axes[axis].min_confidence

    def source_default_confidence(self, axis: str) -> float:
        return self._axes[axis].source_default_confidence

    def is_valid(self, axis: str, value: str) -> bool:
        ax = self._axes.get(axis)
        return ax is not None and value in ax.values

    def axis_values(self, axis: str) -> list[AxisValue]:
        return list(self._axes[axis].values.values())

    def jurisdiction_ids(self) -> set:
        return set(self._jurisdictions.keys())

    def jurisdiction_name(self, jurisdiction_id: str) -> str:
        info = self._jurisdictions.get(jurisdiction_id)
        return info["name"] if info else jurisdiction_id

    def ancestors(self, jurisdiction_id: str) -> list:
        """Transitive closure of `parents`, insertion-ordered, no duplicates.
        Unknown jurisdiction ids simply have no ancestors."""
        result = []
        seen = set()

        def _walk(jid):
            info = self._jurisdictions.get(jid)
            if info is None:
                return
            for parent in info.get("parents", []):
                if parent not in seen:
                    seen.add(parent)
                    result.append(parent)
                    _walk(parent)

        _walk(jurisdiction_id)
        return result


def _load_yaml(path: str):
    try:
        with open(path) as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        raise TaxonomyError(f"{path}: file not found")
    except yaml.YAMLError as e:
        raise TaxonomyError(f"{path}: invalid YAML - {e}")


def _build_axes(path: str, data) -> tuple:
    if not isinstance(data, dict) or "axes" not in data:
        raise TaxonomyError(f"{path}: must be a mapping with a top-level 'axes' list")
    version = data.get("version")
    if not isinstance(version, int):
        raise TaxonomyError(f"{path}: top-level 'version' must be an integer")
    raw_axes = data.get("axes")
    if not isinstance(raw_axes, list) or not raw_axes:
        raise TaxonomyError(f"{path}: 'axes' must be a non-empty list")

    axes: dict = {}
    for raw_axis in raw_axes:
        axis_id = raw_axis.get("id") if isinstance(raw_axis, dict) else None
        if not axis_id:
            raise TaxonomyError(f"{path}: axis entry missing 'id'")
        if axis_id in axes:
            raise TaxonomyError(f"{path}: duplicate axis id '{axis_id}'")
        cardinality = raw_axis.get("cardinality")
        if cardinality not in ("one", "many"):
            raise TaxonomyError(f"{path}: axis '{axis_id}' cardinality must be 'one' or 'many'")
        raw_values = raw_axis.get("values")
        if not isinstance(raw_values, list) or not raw_values:
            raise TaxonomyError(f"{path}: axis '{axis_id}' must have a non-empty 'values' list")
        values: dict = {}
        for raw_value in raw_values:
            value_id = raw_value.get("id") if isinstance(raw_value, dict) else None
            if not value_id:
                raise TaxonomyError(f"{path}: axis '{axis_id}' has a value entry missing 'id'")
            if value_id in values:
                raise TaxonomyError(f"{path}: axis '{axis_id}' has duplicate value id '{value_id}'")
            values[value_id] = AxisValue(
                id=value_id,
                name=raw_value.get("name", value_id),
                aliases=raw_value.get("aliases", []) or [],
                jurisdiction=raw_value.get("jurisdiction"),
            )
        axes[axis_id] = Axis(
            id=axis_id,
            cardinality=cardinality,
            values=values,
            min_confidence=raw_axis.get("min_confidence", 0.5),
            source_default_confidence=raw_axis.get("source_default_confidence", 0.6),
        )
    return version, axes


def _build_jurisdictions(path: str, data) -> dict:
    if not isinstance(data, list) or not data:
        raise TaxonomyError(f"{path}: must be a non-empty list")
    jurisdictions: dict = {}
    for raw in data:
        jid = raw.get("id") if isinstance(raw, dict) else None
        if not jid:
            raise TaxonomyError(f"{path}: jurisdiction entry missing 'id'")
        if jid in jurisdictions:
            raise TaxonomyError(f"{path}: duplicate jurisdiction id '{jid}'")
        jurisdictions[jid] = {
            "name": raw.get("name", jid),
            "type": raw.get("type"),
            "parents": raw.get("parents", []) or [],
            "note": raw.get("note"),
        }
    for jid, info in jurisdictions.items():
        for parent in info["parents"]:
            if parent not in jurisdictions:
                raise TaxonomyError(f"{path}: jurisdiction '{jid}' references unknown parent '{parent}'")

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {jid: WHITE for jid in jurisdictions}

    def _visit(jid, stack):
        if color[jid] == BLACK:
            return
        if color[jid] == GRAY:
            cycle = " -> ".join(stack + [jid])
            raise TaxonomyError(f"{path}: cycle detected in jurisdiction parents: {cycle}")
        color[jid] = GRAY
        for parent in jurisdictions[jid]["parents"]:
            _visit(parent, stack + [jid])
        color[jid] = BLACK

    for jid in jurisdictions:
        _visit(jid, [])

    return jurisdictions


def load_taxonomy(taxonomy_path: str) -> tuple:
    return _build_axes(taxonomy_path, _load_yaml(taxonomy_path))


def load_jurisdictions(jurisdictions_path: str) -> dict:
    return _build_jurisdictions(jurisdictions_path, _load_yaml(jurisdictions_path))


def build_taxonomy(taxonomy_path: str, jurisdictions_path: str) -> Taxonomy:
    """Loads and validates both config files, cross-checking that every
    instrument value's optional `jurisdiction` (informational only - never
    rejected, see architecture doc §4.1) references a known jurisdiction id.
    Raises TaxonomyError naming the file on any structural problem.
    """
    version, axes = load_taxonomy(taxonomy_path)
    jurisdictions = load_jurisdictions(jurisdictions_path)
    for axis in axes.values():
        for value in axis.values.values():
            if value.jurisdiction and value.jurisdiction not in jurisdictions:
                raise TaxonomyError(
                    f"{taxonomy_path}: axis '{axis.id}' value '{value.id}' references "
                    f"unknown jurisdiction '{value.jurisdiction}'"
                )
    return Taxonomy(version, axes, jurisdictions)


def validate_regulator_jurisdictions(regulators: list, taxonomy: Taxonomy, regulators_path: str) -> None:
    """Raises TaxonomyError if any regulator's `jurisdiction` is not a known
    jurisdiction id."""
    known = taxonomy.jurisdiction_ids()
    for reg in regulators:
        jid = (reg.get("jurisdiction") or "").lower() if isinstance(reg, dict) else None
        if jid not in known:
            raise TaxonomyError(
                f"{regulators_path}: regulator '{reg.get('id', '<unknown>')}' has unknown "
                f"jurisdiction '{reg.get('jurisdiction')!r}'"
            )


def load_and_exit_on_error(taxonomy_path: str, jurisdictions_path: str, regulators: list | None = None,
                            regulators_path: str = "") -> Taxonomy:
    """Collector startup entry point: builds the taxonomy or exits the
    process with a clear message naming the offending file, matching
    collector/main.py's existing fail-loudly-on-bad-config convention (see
    load_regulators())."""
    try:
        taxonomy = build_taxonomy(taxonomy_path, jurisdictions_path)
        if regulators is not None:
            validate_regulator_jurisdictions(regulators, taxonomy, regulators_path)
    except TaxonomyError as e:
        logger.error("Invalid taxonomy configuration: %s", e)
        sys.exit(1)
    return taxonomy


if __name__ == "__main__":
    build_taxonomy(
        os.getenv("TAXONOMY_PATH", "config/taxonomy.yaml"),
        os.getenv("JURISDICTIONS_PATH", "config/jurisdictions.yaml"),
    )

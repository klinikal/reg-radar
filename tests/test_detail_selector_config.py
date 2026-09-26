"""Shape validation for `detail_selector` config (#17, config/regulators/*.yaml).

A malformed entry here fails silently at collection time: `select_detail_html`
either raises inside BeautifulSoup on a non-string selector, or a
non-list `detail_exclude` blows up the `for exclude_selector in (...)` loop
in collector.detail. Catching the shape here, once, beats every regulator
discovering it independently in production.
"""
import os

from collector.main import load_regulators


def _registry():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return list(load_regulators(os.path.join(root, "config", "regulators")))


def test_every_detail_selector_is_a_non_empty_string_with_list_exclude():
    regulators = _registry()
    checked = 0
    for reg in regulators:
        if "detail_selector" not in reg:
            continue
        checked += 1
        selector = reg["detail_selector"]
        assert isinstance(selector, str) and selector.strip(), reg["id"]
        exclude = reg.get("detail_exclude")
        if exclude is not None:
            assert isinstance(exclude, list), reg["id"]
    assert checked >= 13, "expected at least the 13 newly surveyed regulators to carry detail_selector"

import pytest

from regradar.profiles import (
    ProfileError,
    expand_profile,
    list_shipped_profiles,
    load_activities,
    load_profile,
)
from regradar.taxonomy import build_taxonomy


def _taxonomy():
    return build_taxonomy("config/taxonomy.yaml", "config/jurisdictions.yaml")


def _activities():
    return load_activities("config/activities.yaml", _taxonomy())


def test_activities_yaml_loads_without_error_and_has_the_four_activities():
    activities = _activities()
    assert set(activities.keys()) == {"equities_dealing", "casp", "emi", "bank"}
    assert "mica" in activities["casp"]["instruments"]


def test_load_activities_rejects_unknown_instrument(tmp_path):
    bad = tmp_path / "activities.yaml"
    bad.write_text("- id: bogus\n  name: Bogus\n  instruments: [not-a-real-instrument]\n")
    with pytest.raises(ProfileError):
        load_activities(str(bad), _taxonomy())


@pytest.mark.parametrize("name", ["uk-equities-dealer", "eu-casp", "eu-emi", "eu-bank"])
def test_shipped_profile_templates_load_without_error(name):
    profile = load_profile(name, _taxonomy(), _activities())
    assert profile["name"] == name


def test_list_shipped_profiles_finds_all_four_templates():
    assert list_shipped_profiles("config/profiles") == ["eu-bank", "eu-casp", "eu-emi", "uk-equities-dealer"]


def test_load_profile_rejects_unknown_jurisdiction(tmp_path):
    bad = tmp_path / "p.yaml"
    bad.write_text("name: bad\njurisdictions: [narnia]\n")
    with pytest.raises(ProfileError):
        load_profile(str(bad), _taxonomy(), _activities())


def test_load_profile_rejects_unknown_activity(tmp_path):
    bad = tmp_path / "p.yaml"
    bad.write_text("name: bad\nactivities: [not-a-real-activity]\n")
    with pytest.raises(ProfileError):
        load_profile(str(bad), _taxonomy(), _activities())


def test_load_profile_rejects_unknown_instrument(tmp_path):
    bad = tmp_path / "p.yaml"
    bad.write_text("name: bad\ninstruments: [not-a-real-instrument]\n")
    with pytest.raises(ProfileError):
        load_profile(str(bad), _taxonomy(), _activities())


def test_load_profile_rejects_unknown_exclude_instrument(tmp_path):
    bad = tmp_path / "p.yaml"
    bad.write_text("name: bad\nexclude_instruments: [not-a-real-instrument]\n")
    with pytest.raises(ProfileError):
        load_profile(str(bad), _taxonomy(), _activities())


def test_load_profile_defaults_include_unclassified_true(tmp_path):
    p = tmp_path / "p.yaml"
    p.write_text("name: minimal\n")
    profile = load_profile(str(p), _taxonomy(), _activities())
    assert profile["include_unclassified"] is True


def test_load_profile_by_literal_path(tmp_path):
    p = tmp_path / "custom.yaml"
    p.write_text("name: custom\njurisdictions: [fr]\n")
    profile = load_profile(str(p), _taxonomy(), _activities())
    assert profile["name"] == "custom"
    assert profile["jurisdictions"] == ["fr"]


def test_expand_profile_jurisdiction_ancestor_union():
    taxonomy = _taxonomy()
    profile = load_profile("uk-equities-dealer", taxonomy, _activities())
    expanded = expand_profile(profile, taxonomy, _activities())
    # Neither entry contributes ancestors: gb has no parents (not an EU
    # member) and intl is a deliberate standalone root with no edges at all
    # (#47), so the set is exactly what the profile names.
    assert expanded.jurisdiction_ids == {"gb", "intl"}


def test_expand_profile_jurisdiction_ancestor_union_for_member_state(tmp_path):
    taxonomy = _taxonomy()
    activities = _activities()
    p = tmp_path / "p.yaml"
    p.write_text("name: p\njurisdictions: [fr]\n")
    expanded = expand_profile(load_profile(str(p), taxonomy, activities), taxonomy, activities)
    # fr's parent is eu - selecting fr also selects its ancestor eu.
    assert expanded.jurisdiction_ids == {"fr", "eu"}


def test_expand_profile_eu_star_descendant_expansion_includes_every_member_state():
    taxonomy = _taxonomy()
    activities = _activities()
    profile = load_profile("eu-casp", taxonomy, activities)
    expanded = expand_profile(profile, taxonomy, activities)
    assert "eu" in expanded.jurisdiction_ids
    assert "fr" in expanded.jurisdiction_ids
    assert "de" in expanded.jurisdiction_ids
    assert "gb" not in expanded.jurisdiction_ids  # gb is not an EU/EEA jurisdiction


def test_expand_profile_plain_jurisdiction_does_not_add_descendants(tmp_path):
    taxonomy = _taxonomy()
    activities = _activities()
    p = tmp_path / "p.yaml"
    p.write_text("name: p\njurisdictions: [eu]\n")
    expanded = expand_profile(load_profile(str(p), taxonomy, activities), taxonomy, activities)
    assert expanded.jurisdiction_ids == {"eu"}  # no member states


def test_expand_profile_instrument_union_of_activities_and_explicit(tmp_path):
    taxonomy = _taxonomy()
    activities = _activities()
    p = tmp_path / "p.yaml"
    p.write_text("name: p\nactivities: [casp]\ninstruments: [uk_fsma]\n")
    expanded = expand_profile(load_profile(str(p), taxonomy, activities), taxonomy, activities)
    assert "mica" in expanded.instrument_ids  # from the casp activity
    assert "uk_fsma" in expanded.instrument_ids  # explicit addition


def test_expand_profile_exclude_instruments_removed_from_the_set():
    taxonomy = _taxonomy()
    activities = _activities()
    profile = load_profile("uk-equities-dealer", taxonomy, activities)
    expanded = expand_profile(profile, taxonomy, activities)
    # §5.2's hard exclude is applied during expansion, so the excluded id is
    # simply absent from the one instrument set - there is no second copy of
    # the exclusion for a query to re-apply.
    assert "mica" not in expanded.instrument_ids
    assert not hasattr(expanded, "exclude_instrument_ids")
    assert "mifid2" in expanded.instrument_ids  # not excluded, still present


def test_expand_profile_include_unclassified_true_branch(tmp_path):
    taxonomy = _taxonomy()
    activities = _activities()
    p = tmp_path / "p.yaml"
    p.write_text("name: p\ninclude_unclassified: true\n")
    expanded = expand_profile(load_profile(str(p), taxonomy, activities), taxonomy, activities)
    assert expanded.include_unclassified is True


def test_expand_profile_include_unclassified_false_branch(tmp_path):
    taxonomy = _taxonomy()
    activities = _activities()
    p = tmp_path / "p.yaml"
    p.write_text("name: p\ninclude_unclassified: false\n")
    expanded = expand_profile(load_profile(str(p), taxonomy, activities), taxonomy, activities)
    assert expanded.include_unclassified is False

"""Per-module poll cadence (issue #49).

The load-bearing assertion is
test_no_cadence_anywhere_is_one_job_on_the_global_schedule: #48's acceptance
criterion is that an install with no cadence opinions behaves exactly as it
did before, and that means exactly one job carrying exactly SCHEDULE.
"""
import logging
import pytest

from collector.main import (
    _group_by_cadence,
    _schedule_collection,
    _acquire_lock,
    _release_lock,
    load_regulators,
    run_collection_locked,
)

REGISTRY = """\
- id: esma
  jurisdiction: eu
  sources:
  - {label: news, type: rss}
- id: fr-amf
  jurisdiction: fr
  sources:
  - {label: news, type: rss}
- id: us-occ
  jurisdiction: us
  sources:
  - {label: news, type: rss}
"""

JURISDICTIONS = """\
- {id: eu, name: European Union}
- {id: fr, name: France, parents: [eu]}
- {id: us, name: United States}
"""


class FakeScheduler:
    """Enough of BlockingScheduler for _schedule_collection - it only ever
    calls add_job()."""

    def __init__(self):
        self.jobs = []

    def add_job(self, func, trigger, args=None, id=None):
        self.jobs.append({"func": func, "trigger": trigger, "args": args, "id": id})


@pytest.fixture
def config(tmp_path):
    (tmp_path / "regulators").mkdir()
    (tmp_path / "regulators" / "all.yaml").write_text(REGISTRY)
    (tmp_path / "jurisdictions.yaml").write_text(JURISDICTIONS)
    return tmp_path


def _resolve(config, modules_yaml):
    (config / "modules.yaml").write_text(modules_yaml)
    return load_regulators(
        str(config / "regulators"),
        modules_path=str(config / "modules.yaml"),
        jurisdictions_path=str(config / "jurisdictions.yaml"),
    )


def _schedule(regulators, schedule="0 6 * * *"):
    scheduler = FakeScheduler()
    cfg = {"schedule": schedule}
    _schedule_collection(scheduler, "<engine>", regulators, cfg, "/tmp/lock", None, None)
    return scheduler.jobs


def _job_map(jobs):
    """job id -> the regulator ids that job collects."""
    return {j["id"]: [r["id"] for r in j["args"][1]] for j in jobs}


# ---------------------------------------------------------------------------
# Cadence resolution (#48's order, applied to `schedule:`)
# ---------------------------------------------------------------------------

def test_cadence_resolves_defaults_then_wildcard_then_module(config):
    regulators = _resolve(config, """
version: 1
defaults:
  schedule: "0 6 * * *"
modules:
  - id: eu/*
    schedule: "0 6 * * 1"
  - id: fr-amf
    schedule: "0 */4 * * *"
  - id: us-occ
""")
    cadences = {r["id"]: r["module"]["schedule"] for r in regulators}
    assert cadences == {
        # wildcard beats defaults
        "esma": "0 6 * * 1",
        # explicit module id beats the wildcard it also matched
        "fr-amf": "0 */4 * * *",
        # nothing narrower set it, so defaults stands
        "us-occ": "0 6 * * *",
    }


# ---------------------------------------------------------------------------
# One job per distinct expression
# ---------------------------------------------------------------------------

def test_three_distinct_cadences_produce_three_jobs_bound_to_their_modules(config):
    regulators = _resolve(config, """
version: 1
defaults:
  schedule: "0 6 * * *"
modules:
  - id: eu/*
    schedule: "0 6 * * 1"
  - id: fr-amf
    schedule: "0 */4 * * *"
  - id: us-occ
""")
    jobs = _schedule(regulators)
    assert len(jobs) == 3
    assert _job_map(jobs) == {
        "collect:0 6 * * 1": ["esma"],
        "collect:0 */4 * * *": ["fr-amf"],
        "collect:0 6 * * *": ["us-occ"],
    }


def test_modules_sharing_an_expression_share_one_job(config):
    """Grouping is by expression, not by module - otherwise the job count
    grows with the registry."""
    regulators = _resolve(config, """
version: 1
defaults:
  schedule: "0 6 * * *"
modules:
  - id: eu/*
  - id: us-occ
    schedule: "0 6 * * *"
""")
    jobs = _schedule(regulators)
    assert len(jobs) == 1
    assert _job_map(jobs) == {"collect:0 6 * * *": ["esma", "fr-amf", "us-occ"]}


def test_no_cadence_anywhere_is_one_job_on_the_global_schedule(config):
    """No modules.yaml cadence at all - the pre-#49 behaviour, preserved
    exactly: one job, carrying SCHEDULE, collecting everything selected."""
    regulators = _resolve(config, """
version: 1
modules:
  - id: eu/*
  - id: us-occ
""")
    assert all((r.get("module") or {}).get("schedule") is None for r in regulators)
    jobs = _schedule(regulators, schedule="15 3 * * *")
    assert len(jobs) == 1
    assert _job_map(jobs) == {"collect:15 3 * * *": ["esma", "fr-amf", "us-occ"]}
    # And the same holds with no module block at all (REGRADAR_COLLECT_ALL /
    # every non-collector caller), where regulators carry no "module" key.
    bare = load_regulators(str(config / "regulators"))
    assert _group_by_cadence(bare, "15 3 * * *") == {"15 3 * * *": bare}


# ---------------------------------------------------------------------------
# Bad cron
# ---------------------------------------------------------------------------

def test_invalid_module_cron_skips_only_that_group(caplog):
    regulators = [
        {"id": "us-occ", "module": {"id": "us-occ", "schedule": "not a cron"}},
        {"id": "esma", "module": {"id": "eu/*", "schedule": "0 6 * * 1"}},
        {"id": "fr-amf", "module": {"id": "fr-amf", "schedule": None}},
    ]
    with caplog.at_level(logging.ERROR):
        jobs = _schedule(regulators)
    assert _job_map(jobs) == {
        "collect:0 6 * * 1": ["esma"],
        "collect:0 6 * * *": ["fr-amf"],
    }
    assert "us-occ" in caplog.text and "not a cron" in caplog.text


def test_invalid_global_schedule_still_fails_loudly():
    """SCHEDULE is the whole install's cadence - swallowing a broken one
    would start a scheduler that collects nothing, silently."""
    with pytest.raises((ValueError, TypeError)):
        _schedule([{"id": "esma"}], schedule="not a cron")


# ---------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------

def test_lock_skip_names_the_cadence_and_the_modules_it_dropped(tmp_path, caplog):
    """One global lock serialises every cadence, so a skip has to say which
    cadence lost and what it was carrying - that is the only way the
    starvation ceiling is observable before it is a bug report."""
    lock_path = str(tmp_path / "collector.lock")
    held = _acquire_lock(lock_path)
    assert held is not None
    try:
        with caplog.at_level(logging.WARNING):
            run_collection_locked(
                "<engine>", [{"id": "us-occ"}, {"id": "esma"}], {"schedule": "0 6 * * *"},
                lock_path, cadence="0 */4 * * *",
            )
    finally:
        _release_lock(held)
    assert "0 */4 * * *" in caplog.text
    assert "us-occ" in caplog.text and "esma" in caplog.text

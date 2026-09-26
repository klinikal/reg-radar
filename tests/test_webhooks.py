import hashlib
import hmac
import json
import tempfile
from datetime import datetime, timezone

import httpx
import pytest

from collector.schema import Item, make_item_id
from collector.storage import (
    get_webhook_cursor,
    init_db,
    insert_item,
    upsert_regulator,
)
from regradar.taxonomy import build_taxonomy
from regradar.profiles import load_activities
from regradar.webhooks import (
    WebhookConfigError,
    _deliver_with_retry,
    dispatch_all,
    load_subscribers,
    sign_payload,
    subscriber_id,
)


def _engine():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return init_db(f.name)


def _item(item_id, regulator_id="esma"):
    now = datetime.now(timezone.utc)
    return Item(
        id=item_id, regulator_id=regulator_id, source_label="all_news", title="Notice",
        url=f"https://example.com/{item_id}", published_at=now, fetched_at=now,
        date_inferred=False, language="en", content_snippet="...", source_type="rss",
        raw_content="raw", body_md="Body.", extractor="raw/1",
    )


def _taxonomy_and_activities():
    taxonomy = build_taxonomy("config/taxonomy.yaml", "config/jurisdictions.yaml")
    activities = load_activities("config/activities.yaml", taxonomy)
    return taxonomy, activities


def _write_config(tmp_path, entries):
    import yaml
    path = tmp_path / "webhooks.yaml"
    path.write_text(yaml.safe_dump(entries))
    return str(path)


def test_load_subscribers_missing_file_is_noop(tmp_path):
    assert load_subscribers(str(tmp_path / "does-not-exist.yaml")) == []


def test_load_subscribers_rejects_missing_required_fields(tmp_path):
    path = _write_config(tmp_path, [{"profile": "eu-casp", "url": "https://x"}])
    with pytest.raises(WebhookConfigError):
        load_subscribers(path)


def test_load_subscribers_applies_defaults(tmp_path):
    path = _write_config(tmp_path, [{"profile": "eu-casp", "url": "https://x", "secret_env": "S"}])
    subs = load_subscribers(path)
    assert subs[0]["batch_size"] == 25
    assert subs[0]["max_retries"] == 5
    assert subs[0]["backoff_seconds"] == 30


def test_sign_payload_matches_hmac_sha256():
    body = b'{"a": 1}'
    sig = sign_payload(body, "supersecret")
    expected = "sha256=" + hmac.new(b"supersecret", body, hashlib.sha256).hexdigest()
    assert sig == expected


def test_dispatch_delivers_signed_payload_and_advances_cursor(tmp_path, monkeypatch):
    engine = _engine()
    upsert_regulator(engine, {"id": "esma", "name": "ESMA", "full_name": "ESMA", "jurisdiction": "eu"})
    insert_item(engine, _item("i1"))
    monkeypatch.setenv("WEBHOOK_SECRET", "supersecret")

    config_path = _write_config(tmp_path, [
        {"profile": "eu-casp", "url": "https://hooks.example/regradar", "secret_env": "WEBHOOK_SECRET"},
    ])
    taxonomy, activities = _taxonomy_and_activities()

    received = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received["body"] = request.content
        received["signature"] = request.headers["X-Reg-Radar-Signature"]
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    dispatch_all(engine, config_path, taxonomy, activities, "config/profiles", client=client)

    assert received["body"] is not None
    payload = json.loads(received["body"])
    assert [i["id"] for i in payload["items"]] == ["i1"]
    expected_sig = sign_payload(received["body"], "supersecret")
    assert received["signature"] == expected_sig

    sid = subscriber_id({"profile": "eu-casp", "url": "https://hooks.example/regradar"})
    assert get_webhook_cursor(engine, sid) is not None


def test_dispatch_missing_secret_env_skips_without_raising(tmp_path):
    engine = _engine()
    upsert_regulator(engine, {"id": "esma", "name": "ESMA", "full_name": "ESMA", "jurisdiction": "eu"})
    insert_item(engine, _item("i1"))

    config_path = _write_config(tmp_path, [
        {"profile": "eu-casp", "url": "https://hooks.example/regradar", "secret_env": "UNSET_ENV_VAR_XYZ"},
    ])
    taxonomy, activities = _taxonomy_and_activities()

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    dispatch_all(engine, config_path, taxonomy, activities, "config/profiles", client=client)
    assert calls == []


def test_dispatch_rejects_receiver_that_rejects_signature(tmp_path, monkeypatch):
    # A receiver validating the signature and rejecting bad ones should
    # never receive one from us in the first place - this confirms our
    # signature is checkable by a real HMAC verification, not garbage.
    engine = _engine()
    upsert_regulator(engine, {"id": "esma", "name": "ESMA", "full_name": "ESMA", "jurisdiction": "eu"})
    insert_item(engine, _item("i1"))
    monkeypatch.setenv("WEBHOOK_SECRET", "supersecret")

    config_path = _write_config(tmp_path, [
        {"profile": "eu-casp", "url": "https://hooks.example/regradar", "secret_env": "WEBHOOK_SECRET"},
    ])
    taxonomy, activities = _taxonomy_and_activities()

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content
        expected = sign_payload(body, "supersecret")
        if request.headers["X-Reg-Radar-Signature"] != expected:
            return httpx.Response(401)
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    dispatch_all(engine, config_path, taxonomy, activities, "config/profiles", client=client)

    sid = subscriber_id({"profile": "eu-casp", "url": "https://hooks.example/regradar"})
    assert get_webhook_cursor(engine, sid) is not None  # accepted -> cursor advanced


def test_dispatch_retries_with_backoff_and_logs_failure_after_max_retries(tmp_path, monkeypatch, caplog):
    engine = _engine()
    upsert_regulator(engine, {"id": "esma", "name": "ESMA", "full_name": "ESMA", "jurisdiction": "eu"})
    insert_item(engine, _item("i1"))
    monkeypatch.setenv("WEBHOOK_SECRET", "supersecret")

    config_path = _write_config(tmp_path, [
        {
            "profile": "eu-casp", "url": "https://hooks.example/regradar", "secret_env": "WEBHOOK_SECRET",
            "max_retries": 3, "backoff_seconds": 0,
        },
    ])
    taxonomy, activities = _taxonomy_and_activities()

    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with caplog.at_level("ERROR"):
        dispatch_all(engine, config_path, taxonomy, activities, "config/profiles", client=client)

    assert len(attempts) == 3
    assert "delivery failed" in caplog.text

    sid = subscriber_id({"profile": "eu-casp", "url": "https://hooks.example/regradar"})
    assert get_webhook_cursor(engine, sid) is None  # never advanced on failure


def test_dispatch_one_subscriber_failure_does_not_block_another(tmp_path, monkeypatch):
    engine = _engine()
    upsert_regulator(engine, {"id": "esma", "name": "ESMA", "full_name": "ESMA", "jurisdiction": "eu"})
    upsert_regulator(engine, {"id": "uk-fca", "name": "FCA", "full_name": "FCA", "jurisdiction": "gb"})
    insert_item(engine, _item("i-eu", regulator_id="esma"))
    insert_item(engine, _item("i-uk", regulator_id="uk-fca"))
    monkeypatch.setenv("WEBHOOK_SECRET_EU", "s1")
    monkeypatch.setenv("WEBHOOK_SECRET_UK", "s2")

    config_path = _write_config(tmp_path, [
        {
            "profile": "eu-casp", "url": "https://hooks.example/failing", "secret_env": "WEBHOOK_SECRET_EU",
            "max_retries": 1, "backoff_seconds": 0,
        },
        {
            "profile": "uk-equities-dealer", "url": "https://hooks.example/ok", "secret_env": "WEBHOOK_SECRET_UK",
            "max_retries": 1, "backoff_seconds": 0,
        },
    ])
    taxonomy, activities = _taxonomy_and_activities()

    def handler(request: httpx.Request) -> httpx.Response:
        if "failing" in str(request.url):
            return httpx.Response(500)
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    dispatch_all(engine, config_path, taxonomy, activities, "config/profiles", client=client)

    failing_id = subscriber_id({"profile": "eu-casp", "url": "https://hooks.example/failing"})
    ok_id = subscriber_id({"profile": "uk-equities-dealer", "url": "https://hooks.example/ok"})
    assert get_webhook_cursor(engine, failing_id) is None
    assert get_webhook_cursor(engine, ok_id) is not None


def test_dispatch_no_config_file_is_noop(tmp_path):
    engine = _engine()
    taxonomy, activities = _taxonomy_and_activities()
    # Should not raise even though nothing is configured.
    dispatch_all(engine, str(tmp_path / "missing-webhooks.yaml"), taxonomy, activities, "config/profiles")


def test_load_subscribers_malformed_yaml_raises_config_error(tmp_path):
    path = tmp_path / "webhooks.yaml"
    path.write_text("- profile: eu-casp\n  url: [unterminated\n")
    with pytest.raises(WebhookConfigError):
        load_subscribers(str(path))


def test_dispatch_malformed_yaml_logs_error_and_does_not_raise(tmp_path, caplog):
    engine = _engine()
    taxonomy, activities = _taxonomy_and_activities()
    path = tmp_path / "webhooks.yaml"
    path.write_text("- profile: eu-casp\n  url: [unterminated\n")

    with caplog.at_level("ERROR"):
        dispatch_all(engine, str(path), taxonomy, activities, "config/profiles")  # must not raise

    assert str(path) in caplog.text


def test_subscriber_id_prefers_explicit_id_over_derived():
    sub = {"id": "acme-prod", "profile": "eu-casp", "url": "https://hooks.example/a"}
    assert subscriber_id(sub) == "acme-prod"


def test_subscriber_id_falls_back_to_profile_url_when_no_id():
    sub = {"profile": "eu-casp", "url": "https://hooks.example/a"}
    assert subscriber_id(sub) == "eu-casp|https://hooks.example/a"


def test_load_subscribers_rejects_duplicate_ids(tmp_path):
    path = _write_config(tmp_path, [
        {"id": "acme", "profile": "eu-casp", "url": "https://x", "secret_env": "S"},
        {"id": "acme", "profile": "uk-equities-dealer", "url": "https://y", "secret_env": "S"},
    ])
    with pytest.raises(WebhookConfigError):
        load_subscribers(path)


def test_load_subscribers_rejects_explicit_id_colliding_with_derived_id(tmp_path):
    # An explicit id must not collide with another subscriber's fallback
    # profile|url id - otherwise the two would silently share one cursor.
    path = _write_config(tmp_path, [
        {"profile": "eu-casp", "url": "https://x", "secret_env": "S"},
        {"id": "eu-casp|https://x", "profile": "uk-equities-dealer", "url": "https://y", "secret_env": "S"},
    ])
    with pytest.raises(WebhookConfigError):
        load_subscribers(path)


def test_rotating_url_preserves_cursor_when_subscriber_has_explicit_id(tmp_path, monkeypatch):
    # Issue #25: a subscriber id derived from url resets its cursor when
    # url is rotated. An explicit id must survive the rotation.
    engine = _engine()
    upsert_regulator(engine, {"id": "esma", "name": "ESMA", "full_name": "ESMA", "jurisdiction": "eu"})
    insert_item(engine, _item("i1"))
    monkeypatch.setenv("WEBHOOK_SECRET", "supersecret")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    taxonomy, activities = _taxonomy_and_activities()

    old_config = _write_config(tmp_path, [
        {"id": "acme", "profile": "eu-casp", "url": "https://hooks.example/old", "secret_env": "WEBHOOK_SECRET"},
    ])
    dispatch_all(engine, old_config, taxonomy, activities, "config/profiles", client=client)
    cursor_before = get_webhook_cursor(engine, "acme")
    assert cursor_before is not None

    # Rotate the url but keep the same explicit id - the cursor stored
    # under "acme" must still be the one dispatch_one reads next.
    new_config = _write_config(tmp_path, [
        {"id": "acme", "profile": "eu-casp", "url": "https://hooks.example/new", "secret_env": "WEBHOOK_SECRET"},
    ])
    insert_item(engine, _item("i2"))
    dispatch_all(engine, new_config, taxonomy, activities, "config/profiles", client=client)
    assert get_webhook_cursor(engine, "acme") is not None
    assert get_webhook_cursor(engine, "acme") != cursor_before  # advanced further, not reset


def test_fresh_subscriber_without_explicit_id_still_gets_working_cursor(tmp_path, monkeypatch):
    engine = _engine()
    upsert_regulator(engine, {"id": "esma", "name": "ESMA", "full_name": "ESMA", "jurisdiction": "eu"})
    insert_item(engine, _item("i1"))
    monkeypatch.setenv("WEBHOOK_SECRET", "supersecret")

    config_path = _write_config(tmp_path, [
        {"profile": "eu-casp", "url": "https://hooks.example/fresh", "secret_env": "WEBHOOK_SECRET"},
    ])
    taxonomy, activities = _taxonomy_and_activities()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    dispatch_all(engine, config_path, taxonomy, activities, "config/profiles", client=client)

    sid = subscriber_id({"profile": "eu-casp", "url": "https://hooks.example/fresh"})
    assert get_webhook_cursor(engine, sid) is not None


def test_backoff_delays_double_between_retries():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    sleeps = []
    ok = _deliver_with_retry(
        client, "https://hooks.example/x", b"{}", "sha256=x",
        max_retries=4, backoff_seconds=2, sleep_fn=sleeps.append,
    )
    assert ok is False
    assert sleeps == [2, 4, 8]


def test_deliver_with_retry_stops_at_run_budget_for_single_dead_subscriber():
    # Issue #19: without a budget, max_retries=5 with doubling backoff from
    # 30s sleeps ~450s (30+60+120+240) for one dead subscriber. A 60s
    # deadline must cut that off after the first sleep, not run to exhaustion.
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    sleeps = []
    fake_time = [0.0]

    def clock_fn():
        return fake_time[0]

    def sleep_fn(seconds):
        sleeps.append(seconds)
        fake_time[0] += seconds

    ok = _deliver_with_retry(
        client, "https://hooks.example/x", b"{}", "sha256=x",
        max_retries=5, backoff_seconds=30, sleep_fn=sleep_fn, clock_fn=clock_fn,
        deadline=60.0,
    )

    assert ok is False
    assert len(attempts) == 2  # not 5 - stopped once the next backoff would cross the deadline
    assert sleeps == [30]
    assert fake_time[0] <= 60.0


def test_dispatch_all_shares_one_run_budget_across_dead_subscribers(tmp_path, monkeypatch, caplog):
    # Several dead subscribers must not each get their own full retry
    # budget: the deadline is shared across the whole dispatch_all() run, so
    # once it's spent, later subscribers are skipped rather than retried.
    engine = _engine()
    upsert_regulator(engine, {"id": "esma", "name": "ESMA", "full_name": "ESMA", "jurisdiction": "eu"})
    insert_item(engine, _item("i1"))
    monkeypatch.setenv("WEBHOOK_SECRET_1", "s1")
    monkeypatch.setenv("WEBHOOK_SECRET_2", "s2")

    config_path = _write_config(tmp_path, [
        {
            "profile": "eu-casp", "url": "https://hooks.example/dead-1", "secret_env": "WEBHOOK_SECRET_1",
            "max_retries": 5, "backoff_seconds": 0,
        },
        {
            "profile": "eu-casp", "url": "https://hooks.example/dead-2", "secret_env": "WEBHOOK_SECRET_2",
            "max_retries": 5, "backoff_seconds": 0,
        },
    ])
    taxonomy, activities = _taxonomy_and_activities()

    attempts_by_url = {"https://hooks.example/dead-1": [], "https://hooks.example/dead-2": []}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts_by_url[str(request.url)].append(1)
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    # Each clock_fn() call simulates 25s having elapsed (e.g. a slow dead
    # endpoint) so the 60s run budget is exhausted partway through the
    # first subscriber's retries, well before either subscriber reaches
    # its own max_retries=5.
    fake_time = [0.0]

    def clock_fn():
        fake_time[0] += 25.0
        return fake_time[0]

    with caplog.at_level("WARNING"):
        dispatch_all(engine, config_path, taxonomy, activities, "config/profiles", client=client,
                     run_budget_seconds=60, clock_fn=clock_fn)

    total_attempts = len(attempts_by_url["https://hooks.example/dead-1"]) + len(attempts_by_url["https://hooks.example/dead-2"])
    # 2 subscribers * max_retries=5 would be 10 attempts without a shared
    # budget; the shared 60s deadline must keep this well under that.
    assert total_attempts < 10
    assert len(attempts_by_url["https://hooks.example/dead-2"]) == 0  # second subscriber skipped entirely
    assert "run budget" in caplog.text

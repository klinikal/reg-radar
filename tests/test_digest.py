import hashlib
import hmac
import json
import tempfile
from datetime import datetime, timezone

import httpx
import pytest

from collector.schema import Item, make_item_id
from collector.storage import get_digest_cursor, init_db, insert_item, upsert_regulator
from regradar.classification import write_classification
from regradar.digest import (
    DigestConfigError,
    load_digests,
    render_digest,
    render_html,
    render_markdown,
    send_digest,
)
from regradar.profiles import expand_profile, get_expanded_profile, load_activities
from regradar.taxonomy import build_taxonomy
from regradar.webhooks import sign_payload


def _engine():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return init_db(f.name)


def _taxonomy_and_activities():
    taxonomy = build_taxonomy("config/taxonomy.yaml", "config/jurisdictions.yaml")
    activities = load_activities("config/activities.yaml", taxonomy)
    return taxonomy, activities


def _gb_profile(taxonomy, activities, instruments=None):
    return expand_profile(
        {
            "name": "gb-all", "jurisdictions": ["gb"], "activities": [], "instruments": instruments or [],
            "exclude_instruments": [], "document_types": [], "regulators": [], "languages": [],
            "include_unclassified": True,
        },
        taxonomy, activities,
    )


def _item(engine, taxonomy, title="Notice", regulator_id="fca", doc_type=None, instrument=None, body="Body text of the notice."):
    upsert_regulator(engine, {
        "id": regulator_id, "name": regulator_id.upper(), "full_name": "Financial Conduct Authority",
        "jurisdiction": "GB", "jurisdiction_type": "national", "website": "https://fca.org.uk",
        "mica_competent_authority": False, "enabled": True,
    })
    now = datetime.now(timezone.utc)
    item = Item(
        id=make_item_id(regulator_id, f"https://example.com/{title}", now),
        regulator_id=regulator_id, source_label="news", title=title,
        url=f"https://example.com/{title}", published_at=now, fetched_at=now,
        date_inferred=False, language="en", content_snippet="snip",
        source_type="scrape", raw_content=f"<p>{body}</p>", body_md=body, extractor="raw/1",
    )
    insert_item(engine, item)
    if doc_type:
        write_classification(engine, taxonomy, item.id, "document_type", doc_type, by="test", confidence=1.0)
    if instrument:
        write_classification(engine, taxonomy, item.id, "instrument", instrument, by="test", confidence=1.0)
    return item.id


# ---------------------------------------------------------------------------
# render_digest / rendering
# ---------------------------------------------------------------------------

def test_render_digest_groups_by_jurisdiction_then_regulator_with_required_fields():
    engine = _engine()
    taxonomy, activities = _taxonomy_and_activities()
    _item(engine, taxonomy, title="CP notice", doc_type="consultation", instrument="mica")
    profile = _gb_profile(taxonomy, activities, instruments=["mica"])

    result = render_digest(engine, profile, "gb-all", since=None)

    assert len(result["items"]) == 1
    assert "## GB" in result["markdown"]
    assert "### Financial Conduct Authority" in result["markdown"]
    assert "CP notice" in result["markdown"]
    assert "document_type: consultation" in result["markdown"]
    assert "instrument: mica" in result["markdown"]
    assert "Body text of the notice." in result["markdown"]
    assert "https://example.com/CP notice" in result["markdown"]

    assert "GB" in result["html"]
    assert "Financial Conduct Authority" in result["html"]
    assert "CP notice" in result["html"]
    assert result["next_cursor"] is not None


def test_fetch_all_changes_includes_stuck_page_and_logs_error(monkeypatch, caplog):
    """A page whose next_cursor doesn't advance past its own since-cursor
    (the classification updated_at formatting mismatch, fixed separately)
    must not silently drop that page's items from the digest - they're
    still real, undelivered items. _fetch_all_changes should include them
    and log an ERROR naming the stuck cursor and the item ids, then stop.
    """
    import regradar.digest as digest_module

    stuck_items = [{"id": "item-1", "updated_at": "2026-01-01T00:00:00.000000Z"},
                   {"id": "item-2", "updated_at": "2026-01-01T00:00:00.000000Z"}]

    calls = []

    def fake_get_changes(engine, since=None, profile=None, limit=100, include_body=False, taxonomy=None):
        # Always returns the same page with a cursor equal to `since` -
        # exactly the stuck-cursor scenario the guard must handle.
        calls.append(since)
        return stuck_items, "STUCK-CURSOR"

    monkeypatch.setattr(digest_module, "get_changes", fake_get_changes)
    caplog.set_level("ERROR")

    items, cursor = digest_module._fetch_all_changes(engine=object(), expanded_profile=object(), since=None)

    # First call (since=None) is a genuine new page and gets included; the
    # second call (since="STUCK-CURSOR") reports the identical items again
    # (the stuck page) - deduplicated by id, not appended a second time.
    assert len(calls) == 2
    assert items == stuck_items
    assert cursor == "STUCK-CURSOR"
    error_lines = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any("did not advance" in msg and "STUCK-CURSOR" in msg and "item-1" in msg and "item-2" in msg for msg in error_lines)


def test_render_markdown_empty_state():
    md = render_markdown("gb-all", [])
    assert "No new items" in md


def test_render_html_empty_state():
    html = render_html("gb-all", [])
    assert "No new items" in html


# ---------------------------------------------------------------------------
# --preview writes nothing, --send advances cursor only on full success
# ---------------------------------------------------------------------------

def test_preview_does_not_touch_state(tmp_path, capsys):
    db_path = tmp_path / "preview.db"
    engine = init_db(str(db_path))
    taxonomy, activities = _taxonomy_and_activities()
    _item(engine, taxonomy, title="Preview item")

    from regradar.cli import main

    rc = main(["digest", "--preview", "--profile", "uk-equities-dealer", "--db-path", str(db_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Preview item" in out
    assert get_digest_cursor(engine, "anything") is None


def test_send_advances_cursor_only_after_every_channel_succeeds():
    engine = _engine()
    taxonomy, activities = _taxonomy_and_activities()
    _item(engine, taxonomy, title="Deliverable item")

    entry = {
        "name": "gb-digest", "profile": "uk-equities-dealer", "schedule": "0 7 * * *",
        "channels": [{"type": "webhook", "url": "https://example.org/hook", "secret_env": "TEST_DIGEST_SECRET"}],
        "send_empty": False,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    import os
    os.environ["TEST_DIGEST_SECRET"] = "s3cret"
    try:
        ok = send_digest(engine, entry, taxonomy, activities, "config/profiles", webhook_client=client)
    finally:
        os.environ.pop("TEST_DIGEST_SECRET", None)

    assert ok is True
    assert get_digest_cursor(engine, "gb-digest") is not None


def test_send_partial_failure_leaves_cursor_untouched():
    engine = _engine()
    taxonomy, activities = _taxonomy_and_activities()
    _item(engine, taxonomy, title="Partial fail item")

    entry = {
        "name": "gb-digest-partial", "profile": "uk-equities-dealer", "schedule": "0 7 * * *",
        "channels": [
            {"type": "webhook", "url": "https://example.org/ok", "secret_env": "TEST_DIGEST_SECRET_2"},
            {
                "type": "webhook", "url": "https://example.org/bad", "secret_env": "TEST_DIGEST_SECRET_2",
                "max_retries": 1, "backoff_seconds": 0,
            },
        ],
        "send_empty": False,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/bad"):
            return httpx.Response(500)
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    import os
    os.environ["TEST_DIGEST_SECRET_2"] = "s3cret"
    try:
        ok = send_digest(
            engine, entry, taxonomy, activities, "config/profiles",
            webhook_client=client,
        )
    finally:
        os.environ.pop("TEST_DIGEST_SECRET_2", None)

    assert ok is False
    assert get_digest_cursor(engine, "gb-digest-partial") is None


def test_second_send_with_nothing_new_sends_nothing_when_send_empty_false():
    engine = _engine()
    taxonomy, activities = _taxonomy_and_activities()
    _item(engine, taxonomy, title="Only item")

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    entry = {
        "name": "gb-digest-repeat", "profile": "uk-equities-dealer", "schedule": "0 7 * * *",
        "channels": [{"type": "webhook", "url": "https://example.org/hook", "secret_env": "TEST_DIGEST_SECRET_3"}],
        "send_empty": False,
    }

    import os
    os.environ["TEST_DIGEST_SECRET_3"] = "s3cret"
    try:
        ok1 = send_digest(engine, entry, taxonomy, activities, "config/profiles", webhook_client=client)
        assert ok1 is True
        assert len(calls) == 1

        ok2 = send_digest(engine, entry, taxonomy, activities, "config/profiles", webhook_client=client)
        assert ok2 is True
        assert len(calls) == 1  # no second delivery - nothing new, send_empty is False
    finally:
        os.environ.pop("TEST_DIGEST_SECRET_3", None)


def test_missed_run_catches_up_exactly_from_stored_cursor():
    engine = _engine()
    taxonomy, activities = _taxonomy_and_activities()
    _item(engine, taxonomy, title="First run item")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    entry = {
        "name": "gb-digest-catchup", "profile": "uk-equities-dealer", "schedule": "0 7 * * *",
        "channels": [{"type": "webhook", "url": "https://example.org/hook", "secret_env": "TEST_DIGEST_SECRET_4"}],
        "send_empty": False,
    }

    import os
    os.environ["TEST_DIGEST_SECRET_4"] = "s3cret"
    try:
        send_digest(engine, entry, taxonomy, activities, "config/profiles", webhook_client=client)
        cursor_after_first = get_digest_cursor(engine, "gb-digest-catchup")

        # A scheduled run is "missed" - nothing calls send_digest. New items
        # arrive in the meantime.
        _item(engine, taxonomy, title="Missed-run item")
        _item(engine, taxonomy, title="Second missed item")

        # The next run should start exactly at the last successfully sent
        # cursor and pick up both missed items in one go.
        profile = get_expanded_profile("uk-equities-dealer", taxonomy, activities, profiles_dir="config/profiles")
        result = render_digest(engine, profile, "uk-equities-dealer", cursor_after_first)
        assert len(result["items"]) == 2
        assert {i["title"] for i in result["items"]} == {"Missed-run item", "Second missed item"}

        ok = send_digest(engine, entry, taxonomy, activities, "config/profiles", webhook_client=client)
        assert ok is True
        assert get_digest_cursor(engine, "gb-digest-catchup") != cursor_after_first
    finally:
        os.environ.pop("TEST_DIGEST_SECRET_4", None)


# ---------------------------------------------------------------------------
# webhook channel reuses issue #8's signing exactly
# ---------------------------------------------------------------------------

def test_webhook_channel_signature_validates_the_same_way_issue_8_does():
    engine = _engine()
    taxonomy, activities = _taxonomy_and_activities()
    _item(engine, taxonomy, title="Signed item")

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        captured["signature"] = request.headers["X-RegRadar-Signature"]
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    entry = {
        "name": "gb-digest-sig", "profile": "uk-equities-dealer", "schedule": "0 7 * * *",
        "channels": [{"type": "webhook", "url": "https://example.org/hook", "secret_env": "TEST_DIGEST_SECRET_5"}],
        "send_empty": False,
    }

    import os
    os.environ["TEST_DIGEST_SECRET_5"] = "s3cret"
    try:
        send_digest(engine, entry, taxonomy, activities, "config/profiles", webhook_client=client)
    finally:
        os.environ.pop("TEST_DIGEST_SECRET_5", None)

    expected = sign_payload(captured["body"], "s3cret")
    assert captured["signature"] == expected
    # Same construction issue #8's own webhook signature test uses.
    digest = hmac.new(b"s3cret", captured["body"], hashlib.sha256).hexdigest()
    assert captured["signature"] == f"sha256={digest}"


# ---------------------------------------------------------------------------
# email delivery
# ---------------------------------------------------------------------------

class _FakeSMTP:
    sent = []

    starttls_contexts = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.starttls_called = False
        self.login_args = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.starttls_called = True
        _FakeSMTP.starttls_contexts.append(context)

    def login(self, user, password):
        self.login_args = (user, password)

    def sendmail(self, from_addr, to_addrs, msg_str):
        _FakeSMTP.sent.append((from_addr, to_addrs, msg_str))


def test_email_delivery_sends_multipart_with_correct_headers(monkeypatch):
    import smtplib

    from regradar.delivery.email import send_email

    _FakeSMTP.sent = []
    _FakeSMTP.starttls_contexts = []
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    cfg = {
        "host": "smtp.example.org", "port": 587, "user": "bot", "password": "pw",
        "from_addr": "ops@example.org", "starttls": True,
    }

    ok = send_email("dest@example.org", "RegRadar digest: test", "plain text body", "<p>html body</p>", cfg=cfg)

    assert ok is True
    # STARTTLS must verify the certificate and hostname, not just encrypt.
    import ssl
    context = _FakeSMTP.starttls_contexts[0]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert len(_FakeSMTP.sent) == 1
    from_addr, to_addrs, msg_str = _FakeSMTP.sent[0]
    assert from_addr == "ops@example.org"
    assert to_addrs == ["dest@example.org"]
    assert "Subject: RegRadar digest: test" in msg_str
    assert "From: ops@example.org" in msg_str
    assert "To: dest@example.org" in msg_str
    assert "multipart/alternative" in msg_str

    import email
    parsed = email.message_from_string(msg_str)
    parts = {part.get_content_type(): part.get_payload(decode=True).decode("utf-8") for part in parsed.walk() if not part.is_multipart()}
    assert parts["text/plain"] == "plain text body"
    assert parts["text/html"] == "<p>html body</p>"


def test_email_delivery_missing_config_returns_false():
    from regradar.delivery.email import send_email
    ok = send_email("dest@example.org", "subject", "text", "<p>html</p>", cfg={})
    assert ok is False


# ---------------------------------------------------------------------------
# telegram delivery + chunking
# ---------------------------------------------------------------------------

def test_telegram_chunking_splits_over_4096_chars_on_paragraph_boundaries():
    from regradar.delivery.telegram import chunk_text
    paragraph = "x" * 100
    text = "\n\n".join([paragraph] * 60)  # ~6000+ chars, well over the limit
    chunks = chunk_text(text, limit=4096)
    assert len(chunks) > 1
    assert all(len(c) <= 4096 for c in chunks)
    assert "".join(chunks).replace("\n\n", "") == text.replace("\n\n", "")


def test_telegram_delivery_sends_one_request_per_chunk():
    from regradar.delivery.telegram import send_telegram
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    text = "\n\n".join(["y" * 100] * 60)

    ok = send_telegram(-1001234567890, text, bot_token="tok", client=client)

    assert ok is True
    assert len(calls) > 1
    assert all(c["chat_id"] == -1001234567890 for c in calls)


def test_telegram_delivery_rejects_non_numeric_chat_id():
    from regradar.delivery.telegram import send_telegram
    ok = send_telegram("not-a-number", "hi", bot_token="tok", client=httpx.Client())
    assert ok is False


# ---------------------------------------------------------------------------
# digests.yaml config loading and scheduler registration
# ---------------------------------------------------------------------------

def _write_digests(tmp_path, entries):
    import yaml
    path = tmp_path / "digests.yaml"
    path.write_text(yaml.safe_dump(entries))
    return str(path)


def test_load_digests_missing_file_is_noop(tmp_path):
    assert load_digests(str(tmp_path / "missing.yaml")) == []


def test_load_digests_valid_entry(tmp_path):
    path = _write_digests(tmp_path, [{
        "name": "uk-daily", "profile": "uk-equities-dealer", "schedule": "0 7 * * *",
        "channels": [{"type": "telegram", "chat_id": "-1001234567890"}],
    }])
    digests = load_digests(path)
    assert len(digests) == 1
    assert digests[0]["name"] == "uk-daily"
    assert digests[0]["send_empty"] is False


def test_load_digests_rejects_non_numeric_telegram_chat_id(tmp_path):
    path = _write_digests(tmp_path, [{
        "name": "uk-daily", "profile": "uk-equities-dealer", "schedule": "0 7 * * *",
        "channels": [{"type": "telegram", "chat_id": "not-numeric"}],
    }])
    with pytest.raises(DigestConfigError):
        load_digests(path)


def test_load_digests_rejects_missing_channels(tmp_path):
    path = _write_digests(tmp_path, [{
        "name": "uk-daily", "profile": "uk-equities-dealer", "schedule": "0 7 * * *", "channels": [],
    }])
    with pytest.raises(DigestConfigError):
        load_digests(path)


def test_load_digests_rejects_email_channel_without_to(tmp_path):
    path = _write_digests(tmp_path, [{
        "name": "uk-daily", "profile": "uk-equities-dealer", "schedule": "0 7 * * *",
        "channels": [{"type": "email"}],
    }])
    with pytest.raises(DigestConfigError):
        load_digests(path)


def test_load_digests_accepts_valid_email_channel(tmp_path):
    path = _write_digests(tmp_path, [{
        "name": "uk-daily", "profile": "uk-equities-dealer", "schedule": "0 7 * * *",
        "channels": [{"type": "email", "to": "ops@example.org"}],
    }])
    digests = load_digests(path)
    assert digests[0]["channels"][0]["to"] == "ops@example.org"


def test_load_digests_rejects_webhook_channel_without_url_or_secret_env(tmp_path):
    path = _write_digests(tmp_path, [{
        "name": "uk-daily", "profile": "uk-equities-dealer", "schedule": "0 7 * * *",
        "channels": [{"type": "webhook", "url": "https://example.org/hook"}],  # missing secret_env
    }])
    with pytest.raises(DigestConfigError):
        load_digests(path)

    path2 = _write_digests(tmp_path, [{
        "name": "uk-daily-2", "profile": "uk-equities-dealer", "schedule": "0 7 * * *",
        "channels": [{"type": "webhook", "secret_env": "WEBHOOK_SECRET"}],  # missing url
    }])
    with pytest.raises(DigestConfigError):
        load_digests(path2)


def test_load_digests_accepts_valid_webhook_channel(tmp_path):
    path = _write_digests(tmp_path, [{
        "name": "uk-daily", "profile": "uk-equities-dealer", "schedule": "0 7 * * *",
        "channels": [{"type": "webhook", "url": "https://example.org/hook", "secret_env": "WEBHOOK_SECRET"}],
    }])
    digests = load_digests(path)
    assert digests[0]["channels"][0]["url"] == "https://example.org/hook"


def test_scheduler_registers_one_cron_job_per_digest(tmp_path):
    from apscheduler.schedulers.background import BackgroundScheduler

    from collector.main import _schedule_digests

    digests_path = _write_digests(tmp_path, [{
        "name": "uk-daily", "profile": "uk-equities-dealer", "schedule": "0 7 * * *",
        "channels": [{"type": "telegram", "chat_id": "-1001234567890"}],
    }])
    engine = _engine()
    taxonomy, _activities = _taxonomy_and_activities()
    scheduler = BackgroundScheduler()
    cfg = {"digests_path": digests_path, "activities_path": "config/activities.yaml", "profiles_dir": "config/profiles"}

    _schedule_digests(scheduler, engine, cfg, taxonomy)

    assert scheduler.get_job("digest:uk-daily") is not None


def test_scheduler_noop_when_digests_yaml_missing(tmp_path):
    from apscheduler.schedulers.background import BackgroundScheduler

    from collector.main import _schedule_digests

    engine = _engine()
    taxonomy, _activities = _taxonomy_and_activities()
    scheduler = BackgroundScheduler()
    cfg = {"digests_path": str(tmp_path / "missing.yaml")}

    _schedule_digests(scheduler, engine, cfg, taxonomy)  # must not raise

    assert scheduler.get_jobs() == []


def test_scheduler_skips_invalid_digests_yaml_without_crashing(tmp_path):
    from apscheduler.schedulers.background import BackgroundScheduler

    from collector.main import _schedule_digests

    path = tmp_path / "digests.yaml"
    path.write_text("not: a: list\n: broken")
    engine = _engine()
    taxonomy, _activities = _taxonomy_and_activities()
    scheduler = BackgroundScheduler()
    cfg = {"digests_path": str(path)}

    _schedule_digests(scheduler, engine, cfg, taxonomy)  # must not raise

    assert scheduler.get_jobs() == []


def test_digest_preview_documents_carry_jurisdiction_ancestors_and_label_provenance(tmp_path, monkeypatch, capsys):
    """`regradar digest --preview` must pass the taxonomy through to
    render_digest(), or every document it builds loses
    jurisdiction_ancestors and label_provenance."""
    import argparse

    import regradar.digest as digest_mod

    db_path = tmp_path / "digest.db"
    engine = init_db(str(db_path))
    taxonomy, _activities = _taxonomy_and_activities()
    _item(engine, taxonomy, title="AMF notice", regulator_id="fr-amf", doc_type="warning")
    with engine.connect() as conn:
        from sqlalchemy import text
        conn.execute(text("UPDATE regulators SET jurisdiction='fr' WHERE id='fr-amf'"))
        conn.commit()

    captured = {}
    real_render_digest = digest_mod.render_digest

    def spy(*args, **kwargs):
        result = real_render_digest(*args, **kwargs)
        captured["items"] = result["items"]
        return result

    monkeypatch.setattr(digest_mod, "render_digest", spy)

    args = argparse.Namespace(
        profile="eu-casp", name=None, preview=True, send=False, since=None,
        digests_path="config/digests.yaml", db_path=str(db_path),
        taxonomy_path="config/taxonomy.yaml", jurisdictions_path="config/jurisdictions.yaml",
        activities_path="config/activities.yaml", profiles_dir="config/profiles",
    )
    assert digest_mod.run(args) == 0

    doc = captured["items"][0]
    assert doc["jurisdiction_ancestors"] == ["eu"]
    assert [(p["axis"], p["value"]) for p in doc["label_provenance"]] == [("document_type", "warning")]

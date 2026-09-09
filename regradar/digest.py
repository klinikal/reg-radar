"""`regradar digest --profile <p> --preview|--send` (docs/issues/11-
digests-and-notifications.md, architecture doc section 6).

Wired into regradar/cli.py's subcommand registry (add_subparser()/run() -
see that module's docstring for the convention).

render_digest() turns everything a profile's cursor hasn't seen yet into a
Markdown and an HTML digest, grouped by jurisdiction then regulator - the
same get_changes() primitive regradar.exporter and regradar.webhooks build
on. `--preview` only renders; `--send` also delivers to config/digests.yaml's
configured channels (regradar.delivery) and, only once every channel
succeeds, advances the digest's own stored cursor (collector.storage's
digest_state table - the same mechanism issue #8's webhook subscribers use,
per this task's ruling). A partial failure leaves the cursor untouched so
the whole digest - not just the failed channel - is retried in full next
run, matching the issue's explicit "no per-channel reconciliation" call.

Deviation from the issue's design-note pseudocode: render_digest() calls
get_changes() with include_body=True (not False as sketched) because the
acceptance criteria requires a one-line snippet per item, which only the
body text can supply; the digest itself never includes the full body, only
a truncated snippet, so this doesn't defeat the "hot path stays small"
intent behind include_body.
"""
import argparse
import html as html_escape_mod
import logging
import os
import sys

import yaml
from sqlalchemy import create_engine

from collector.storage import get_digest_cursor, set_digest_cursor
from mcpserver.queries import get_changes
from regradar.profiles import get_expanded_profile, load_activities
from regradar.taxonomy import load_and_exit_on_error

logger = logging.getLogger(__name__)

_PAGE_SIZE = 200
_SNIPPET_LENGTH = 200
DEFAULT_DIGESTS_PATH = "config/digests.yaml"
_VALID_CHANNEL_TYPES = {"email", "telegram", "webhook"}


class DigestConfigError(ValueError):
    """Raised when config/digests.yaml is structurally invalid."""


def load_digests(path: str) -> list[dict]:
    """Returns [] if `path` doesn't exist - a missing digests.yaml is a
    no-op, not an error (digests are opt-in, matching webhooks.yaml's own
    precedent). Each entry: {name, profile, schedule, channels,
    send_empty}."""
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise DigestConfigError(f"{path}: invalid YAML - {e}")
    if data is None:
        return []
    if not isinstance(data, list):
        raise DigestConfigError(f"{path}: must be a list of digest entries")

    digests = []
    for i, raw in enumerate(data):
        if not isinstance(raw, dict) or not raw.get("name") or not raw.get("profile") or not raw.get("schedule"):
            raise DigestConfigError(f"{path}: entry {i} must have name, profile and schedule")
        channels = raw.get("channels") or []
        if not isinstance(channels, list) or not channels:
            raise DigestConfigError(f"{path}: digest '{raw['name']}' must have at least one channel")
        for c in channels:
            if not isinstance(c, dict) or c.get("type") not in _VALID_CHANNEL_TYPES:
                raise DigestConfigError(f"{path}: digest '{raw['name']}' has a channel with an unknown type")
            if c["type"] == "telegram":
                try:
                    int(c.get("chat_id"))
                except (TypeError, ValueError):
                    raise DigestConfigError(f"{path}: digest '{raw['name']}' telegram channel needs a numeric chat_id")
        digests.append({
            "name": raw["name"],
            "profile": raw["profile"],
            "schedule": raw["schedule"],
            "channels": channels,
            "send_empty": bool(raw.get("send_empty", False)),
        })
    return digests


def _snippet(body_md: str) -> str:
    flat = " ".join((body_md or "").split())
    if len(flat) <= _SNIPPET_LENGTH:
        return flat
    return flat[:_SNIPPET_LENGTH].rsplit(" ", 1)[0] + "..."


def _label_str(labels: dict, axis: str) -> str:
    value = (labels or {}).get(axis)
    if not value:
        return ""
    return ", ".join(value) if isinstance(value, list) else str(value)


def _fetch_all_changes(engine, expanded_profile, since: str | None) -> tuple[list[dict], str | None]:
    """Pages through get_changes() until nothing new is left, returning
    every matching item plus the final cursor - the same pagination shape
    regradar.exporter.export_profile() uses.

    Stops (rather than looping forever) if a page's next_cursor doesn't
    advance past the cursor it was fetched with: items reclassified via
    regradar.classification (as opposed to freshly inserted) can end up
    with an items.updated_at value stored in a different text format than
    the one a cursor round-trips to, which otherwise makes such a row look
    "after itself" indefinitely. This guard is defensive, not a fix for
    that upstream formatting mismatch, which is out of this task's scope.
    """
    all_items: list[dict] = []
    cursor = since
    while True:
        items, next_cursor = get_changes(engine, since=cursor, profile=expanded_profile, limit=_PAGE_SIZE, include_body=True)
        if not items or next_cursor == cursor:
            break
        all_items.extend(items)
        cursor = next_cursor
    return all_items, cursor


def _group(items: list[dict]) -> dict:
    """{jurisdiction: {regulator_name: [item, ...]}}, both levels sorted
    alphabetically for deterministic output."""
    groups: dict = {}
    for item in items:
        jurisdiction = item.get("jurisdiction") or "unknown"
        regulator = item.get("regulator_name") or item.get("regulator") or "unknown"
        groups.setdefault(jurisdiction, {}).setdefault(regulator, []).append(item)
    return {j: {r: groups[j][r] for r in sorted(groups[j])} for j in sorted(groups)}


def render_markdown(profile_name: str, items: list[dict]) -> str:
    lines = [f"# RegRadar digest: {profile_name}", ""]
    if not items:
        lines.append("No new items since the last digest.")
        return "\n".join(lines) + "\n"

    lines.append(f"{len(items)} new item(s).")
    for jurisdiction, regulators in _group(items).items():
        lines.append("")
        lines.append(f"## {jurisdiction.upper()}")
        for regulator, reg_items in regulators.items():
            lines.append("")
            lines.append(f"### {regulator}")
            for item in reg_items:
                doc_type = _label_str(item.get("labels"), "document_type") or "unclassified"
                instrument = _label_str(item.get("labels"), "instrument")
                meta = f"document_type: {doc_type}" + (f", instrument: {instrument}" if instrument else "")
                snippet = _snippet(item.get("body_md", ""))
                lines.append(f"- **{item.get('title', '')}** ({meta})")
                if snippet:
                    lines.append(f"  {snippet}")
                lines.append(f"  [{item.get('source_url', '')}]({item.get('source_url', '')})")
    return "\n".join(lines) + "\n"


def render_html(profile_name: str, items: list[dict]) -> str:
    esc = html_escape_mod.escape
    parts = [
        '<div style="font-family: sans-serif; color: #1a1a1a;">',
        f"<h1>RegRadar digest: {esc(profile_name)}</h1>",
    ]
    if not items:
        parts.append("<p>No new items since the last digest.</p></div>")
        return "".join(parts)

    parts.append(f"<p>{len(items)} new item(s).</p>")
    for jurisdiction, regulators in _group(items).items():
        parts.append(f"<h2>{esc(jurisdiction.upper())}</h2>")
        for regulator, reg_items in regulators.items():
            parts.append(f"<h3>{esc(regulator)}</h3>")
            parts.append('<ul style="padding-left: 1.2em;">')
            for item in reg_items:
                doc_type = _label_str(item.get("labels"), "document_type") or "unclassified"
                instrument = _label_str(item.get("labels"), "instrument")
                meta = f"document_type: {doc_type}" + (f", instrument: {instrument}" if instrument else "")
                snippet = _snippet(item.get("body_md", ""))
                url = item.get("source_url", "")
                parts.append(
                    "<li>"
                    f'<strong>{esc(item.get("title", ""))}</strong> '
                    f'<span style="color:#666;">({esc(meta)})</span><br>'
                    + (f"{esc(snippet)}<br>" if snippet else "")
                    + f'<a href="{esc(url)}">{esc(url)}</a>'
                    "</li>"
                )
            parts.append("</ul>")
    parts.append("</div>")
    return "".join(parts)


def render_digest(engine, expanded_profile, profile_name: str, since: str | None) -> dict:
    """Returns {"markdown", "html", "next_cursor", "items"} for everything
    matching `expanded_profile` changed after `since`. `next_cursor` is
    `since` unchanged when nothing new was found."""
    items, next_cursor = _fetch_all_changes(engine, expanded_profile, since)
    return {
        "markdown": render_markdown(profile_name, items),
        "html": render_html(profile_name, items),
        "next_cursor": next_cursor,
        "items": items,
    }


def _deliver_channel(engine, digest_name: str, channel: dict, markdown: str, html: str,
                      items: list[dict], next_cursor: str | None, since: str | None, *,
                      telegram_client=None, webhook_client=None) -> bool:
    ctype = channel["type"]
    if ctype == "email":
        from regradar.delivery.email import send_email
        return send_email(channel["to"], f"RegRadar digest: {digest_name}", markdown, html)
    if ctype == "telegram":
        from regradar.delivery.telegram import send_telegram
        return send_telegram(channel["chat_id"], markdown, client=telegram_client)
    if ctype == "webhook":
        from regradar.delivery.webhook import send_webhook
        from regradar.webhooks import DEFAULT_BACKOFF_SECONDS, DEFAULT_MAX_RETRIES
        secret = os.getenv(channel["secret_env"])
        if not secret:
            logger.error("Digest '%s' webhook channel: env var %s is not set - skipping", digest_name, channel["secret_env"])
            return False
        payload = {"digest": digest_name, "cursor": since, "next_cursor": next_cursor, "items": items}
        client = webhook_client
        owns_client = client is None
        if owns_client:
            import httpx
            client = httpx.Client(timeout=30)
        try:
            return send_webhook(
                client, channel["url"], secret, payload,
                max_retries=int(channel.get("max_retries", DEFAULT_MAX_RETRIES)),
                backoff_seconds=int(channel.get("backoff_seconds", DEFAULT_BACKOFF_SECONDS)),
            )
        finally:
            if owns_client:
                client.close()
    logger.error("Digest '%s': unknown channel type %r", digest_name, ctype)
    return False


def send_digest(engine, entry: dict, taxonomy, activities: dict, profiles_dir: str, *,
                 telegram_client=None, webhook_client=None) -> bool:
    """Renders and delivers one digests.yaml entry to every configured
    channel, advancing its stored cursor only if every channel succeeds.
    Returns True if the digest is now fully up to date (delivered, or
    nothing new and send_empty is False) - False on any partial failure.
    """
    name = entry["name"]
    expanded_profile = get_expanded_profile(entry["profile"], taxonomy, activities, profiles_dir=profiles_dir)
    since = get_digest_cursor(engine, name)
    result = render_digest(engine, expanded_profile, entry["profile"], since)

    if not result["items"] and not entry["send_empty"]:
        logger.info("Digest '%s': nothing new since last send - skipping (send_empty is false)", name)
        return True

    ok = True
    for channel in entry["channels"]:
        delivered = _deliver_channel(
            engine, name, channel, result["markdown"], result["html"], result["items"],
            result["next_cursor"], since, telegram_client=telegram_client, webhook_client=webhook_client,
        )
        if not delivered:
            ok = False

    if ok:
        if result["next_cursor"] and result["next_cursor"] != since:
            set_digest_cursor(engine, name, result["next_cursor"])
        logger.info("Digest '%s': delivered %d item(s) to %d channel(s)", name, len(result["items"]), len(entry["channels"]))
    else:
        logger.error("Digest '%s': at least one channel failed - cursor not advanced, retried in full next run", name)
    return ok


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("digest", help="Render or send a profile digest (email/Telegram/webhook)")
    parser.add_argument("--profile", help="Profile name or path (required with --preview; overrides the digests.yaml entry's profile with --send --name)")
    parser.add_argument("--name", help="Digest name from --digests-path (required with --send)")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true", help="Render and print the digest; sends nothing")
    mode.add_argument("--send", action="store_true", help="Render and deliver the named digest's channels")
    parser.add_argument("--since", help="Cursor to render from with --preview (default: full history)")
    parser.add_argument("--digests-path", default=os.getenv("DIGESTS_PATH", DEFAULT_DIGESTS_PATH))
    parser.add_argument("--db-path", default=os.getenv("DB_PATH", "/app/data/regradar.db"))
    parser.add_argument("--taxonomy-path", default=os.getenv("TAXONOMY_PATH", "config/taxonomy.yaml"))
    parser.add_argument("--jurisdictions-path", default=os.getenv("JURISDICTIONS_PATH", "config/jurisdictions.yaml"))
    parser.add_argument("--activities-path", default=os.getenv("ACTIVITIES_PATH", "config/activities.yaml"))
    parser.add_argument("--profiles-dir", default=os.getenv("PROFILES_DIR", "config/profiles"))
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    engine = create_engine(f"sqlite:///{args.db_path}")
    taxonomy = load_and_exit_on_error(args.taxonomy_path, args.jurisdictions_path)
    activities = load_activities(args.activities_path, taxonomy)

    if args.preview:
        if not args.profile:
            print("regradar digest --preview requires --profile", file=sys.stderr)
            return 1
        try:
            expanded_profile = get_expanded_profile(args.profile, taxonomy, activities, profiles_dir=args.profiles_dir)
        except Exception as e:
            print(f"regradar digest: {e}", file=sys.stderr)
            return 1
        result = render_digest(engine, expanded_profile, args.profile, args.since)
        print(result["markdown"])
        return 0

    # --send
    try:
        digests = load_digests(args.digests_path)
    except DigestConfigError as e:
        print(f"regradar digest: {e}", file=sys.stderr)
        return 1
    if args.name:
        matches = [d for d in digests if d["name"] == args.name]
        if not matches:
            print(f"regradar digest: no entry named {args.name!r} in {args.digests_path}", file=sys.stderr)
            return 1
        digests = matches
    elif not digests:
        print(f"regradar digest: no digests configured in {args.digests_path}", file=sys.stderr)
        return 0

    all_ok = True
    for entry in digests:
        if not send_digest(engine, entry, taxonomy, activities, args.profiles_dir):
            all_ok = False
    return 0 if all_ok else 1

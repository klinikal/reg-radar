"""Digest delivery via SMTP (issue #13). Config comes from environment
variables, matching the `.env.example` convention: `SMTP_HOST`,
`SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD_ENV` (the name of the env var
holding the password - the password itself is never in config, matching
regradar.webhooks' `secret_env` pattern), `SMTP_FROM`, `SMTP_STARTTLS`.
"""
import logging
import os
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


def smtp_config_from_env() -> dict:
    return {
        "host": os.getenv("SMTP_HOST"),
        "port": int(os.getenv("SMTP_PORT", "587")),
        "user": os.getenv("SMTP_USER") or None,
        "password": os.getenv(os.getenv("SMTP_PASSWORD_ENV", "")) or None,
        "from_addr": os.getenv("SMTP_FROM"),
        "starttls": os.getenv("SMTP_STARTTLS", "true").strip().lower() == "true",
    }


def send_email(to: str, subject: str, text_body: str, html_body: str, cfg: dict | None = None) -> bool:
    """Sends a multipart/alternative message (plain text + HTML parts) to
    `to`. Returns False (logging why) rather than raising on any config or
    delivery failure, matching regradar.webhooks' "never raise for an
    expected failure mode" convention.
    """
    cfg = cfg if cfg is not None else smtp_config_from_env()
    if not cfg.get("host") or not cfg.get("from_addr"):
        logger.error("Email delivery to %s: SMTP_HOST/SMTP_FROM not configured - skipping", to)
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg["from_addr"]
    msg["To"] = to
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as server:
            if cfg.get("starttls"):
                # A bare starttls() skips certificate and hostname
                # verification, so the "encrypted" session is open to a
                # man-in-the-middle - the default SSL context checks both.
                server.starttls(context=ssl.create_default_context())
            if cfg.get("user") and cfg.get("password"):
                server.login(cfg["user"], cfg["password"])
            server.sendmail(cfg["from_addr"], [to], msg.as_string())
        return True
    except (smtplib.SMTPException, OSError) as e:
        logger.error("Email delivery to %s failed: %s", to, e)
        return False

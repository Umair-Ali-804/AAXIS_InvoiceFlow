"""Email intake: parse forwarded .eml files and poll an IMAP inbox (e.g. ap@company.com)."""
from __future__ import annotations

import email
import imaplib
from email import policy
from email.message import EmailMessage
from typing import Any

from .config import settings

ACCEPTED = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}


def parse_eml(raw: bytes) -> dict[str, Any]:
    message: EmailMessage = email.message_from_bytes(raw, policy=policy.default)  # type: ignore[assignment]
    attachments = []
    for part in message.iter_attachments():
        name = part.get_filename() or ""
        if any(name.lower().endswith(ext) for ext in ACCEPTED):
            attachments.append({"filename": name, "content": part.get_payload(decode=True)})
    return {
        "from": str(message.get("From", "")),
        "subject": str(message.get("Subject", "")),
        "date": str(message.get("Date", "")),
        "message_id": str(message.get("Message-ID", "")),
        "attachments": attachments,
    }


def poll_inbox(limit: int = 20) -> list[dict[str, Any]]:
    """Fetch unread messages with invoice attachments and mark them read."""
    if not settings.imap_configured:
        raise RuntimeError("Email intake is not configured. Set IMAP_HOST, IMAP_USER and IMAP_PASSWORD.")
    messages = []
    with imaplib.IMAP4_SSL(settings.imap_host) as client:
        client.login(settings.imap_user, settings.imap_password)
        client.select(settings.imap_folder)
        _, data = client.search(None, "UNSEEN")
        for num in data[0].split()[:limit]:
            _, parts = client.fetch(num, "(RFC822)")
            raw = parts[0][1] if parts and isinstance(parts[0], tuple) else b""
            parsed = parse_eml(raw)
            if parsed["attachments"]:
                messages.append(parsed)
            client.store(num, "+FLAGS", "\\Seen")
    return messages

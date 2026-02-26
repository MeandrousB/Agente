from __future__ import annotations

from datetime import datetime
from hashlib import sha1
from typing import Any

from .models import NormalizedMessage

NOISE_TERMS = (
    "mensagem apagada",
    "apagou esta mensagem",
    "mudou a foto do grupo",
    "mudou o assunto",
    "entrou usando o link",
)


def _parse_timestamp(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _fallback_external_id(author: str, timestamp: str | datetime, text: str) -> str:
    base = f"{author}|{timestamp}|{text}".encode("utf-8")
    return sha1(base).hexdigest()


def normalize_raw_message(group_name: str, raw: dict[str, Any]) -> NormalizedMessage | None:
    text = (raw.get("text") or "").strip()
    if not text:
        return None

    lowered = text.lower()
    if any(term in lowered for term in NOISE_TERMS):
        return None

    author = (raw.get("author") or "desconhecido").strip()
    timestamp = _parse_timestamp(raw["timestamp"])

    return NormalizedMessage(
        group_name=group_name,
        author=author,
        timestamp=timestamp,
        text=text,
        external_id=str(raw.get("external_id") or _fallback_external_id(author, timestamp.isoformat(), text)),
        reply_to=raw.get("reply_to"),
        attachments=list(raw.get("attachments") or []),
    )

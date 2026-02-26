from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class NormalizedMessage:
    group_name: str
    author: str
    timestamp: datetime
    text: str
    external_id: str
    reply_to: str | None = None
    attachments: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class SummaryState:
    decisions: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    current_status: str = "Sem status consolidado"


@dataclass(slots=True)
class SummaryResult:
    summary_text: str
    state: SummaryState
    message_count: int

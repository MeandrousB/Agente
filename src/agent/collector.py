from __future__ import annotations

import hashlib
import json
import re
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


class MessageCollector(ABC):
    @abstractmethod
    def collect_messages(self, group_name: str, since_timestamp: datetime | None = None) -> list[dict[str, Any]]:
        """Retorna mensagens brutas coletadas do grupo."""


class MockCollector(MessageCollector):
    def collect_messages(self, group_name: str, since_timestamp: datetime | None = None) -> list[dict[str, Any]]:
        now = datetime.now().replace(microsecond=0)
        sample = [
            {
                "author": "Ana",
                "timestamp": (now - timedelta(minutes=5)).isoformat(),
                "text": "Decisão: subir release na sexta.",
                "external_id": "m1",
            },
            {
                "author": "Bruno",
                "timestamp": (now - timedelta(minutes=4)).isoformat(),
                "text": "Pendente: validar contrato com fornecedor.",
                "external_id": "m2",
            },
            {
                "author": "Carla",
                "timestamp": (now - timedelta(minutes=3)).isoformat(),
                "text": "Risco: atraso na homologação do cliente.",
                "external_id": "m3",
            },
            {
                "author": "Sistema",
                "timestamp": (now - timedelta(minutes=2)).isoformat(),
                "text": "João mudou a foto do grupo",
                "external_id": "m4",
            },
        ]
        return _filter_since(sample, since_timestamp)


class JsonFileCollector(MessageCollector):
    def __init__(self, source_path: str) -> None:
        self.source_path = Path(source_path)

    def collect_messages(self, group_name: str, since_timestamp: datetime | None = None) -> list[dict[str, Any]]:
        if not self.source_path.exists():
            raise FileNotFoundError(f"Arquivo de mensagens não encontrado: {self.source_path}")

        payload = json.loads(self.source_path.read_text(encoding="utf-8"))
        if group_name not in payload:
            available = ", ".join(sorted(payload.keys()))
            raise ValueError(
                f"Grupo '{group_name}' não encontrado no JSON. Grupos disponíveis: {available or '(nenhum)'}"
            )

        group_messages = payload[group_name]
        if not isinstance(group_messages, list):
            raise ValueError(f"Grupo '{group_name}' deve conter uma lista de mensagens no JSON.")

        return _filter_since(group_messages, since_timestamp)


class PlaywrightWhatsAppCollector(MessageCollector):
    """Coletor WhatsApp Web com Playwright (experimental)."""

    def __init__(
        self,
        profile_dir: str,
        headless: bool = False,
        max_messages_visible: int = 300,
        group_search_selector: str = "div[contenteditable='true'][data-tab='3']",
        message_row_selector: str = "div[data-pre-plain-text], div.message-in, div.message-out",
        text_selector: str = "span.selectable-text, span.copyable-text",
    ) -> None:
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self.max_messages_visible = max_messages_visible
        self.group_search_selector = group_search_selector
        self.message_row_selector = message_row_selector
        self.text_selector = text_selector

    def collect_messages(self, group_name: str, since_timestamp: datetime | None = None) -> list[dict[str, Any]]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright não está instalado. Rode: python -m pip install playwright && python -m playwright install chromium"
            ) from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                headless=self.headless,
            )
            page = context.new_page()
            page.goto("https://web.whatsapp.com", wait_until="domcontentloaded")

            self._open_group(page, group_name)
            rows = page.locator(self.message_row_selector)
            count = min(rows.count(), self.max_messages_visible)

            data: list[dict[str, Any]] = []
            for idx in range(count):
                row = rows.nth(idx)
                text = " ".join(row.locator(self.text_selector).all_inner_texts()).strip()
                if not text:
                    text = (row.inner_text(timeout=1000) or "").strip()
                if not text:
                    continue

                meta = row.get_attribute("data-pre-plain-text")
                if not meta:
                    meta = row.locator("[data-pre-plain-text]").first.get_attribute("data-pre-plain-text")
                author, timestamp = extract_author_and_timestamp(meta or "")
                external_seed = f"{author}|{timestamp.isoformat()}|{text}|{idx}"
                external_id = f"pw-{hashlib.sha1(external_seed.encode('utf-8')).hexdigest()[:16]}"
                data.append(
                    {
                        "author": author,
                        "timestamp": timestamp.isoformat(),
                        "text": text,
                        "external_id": external_id,
                    }
                )

            context.close()

        if not data and since_timestamp is None:
            raise RuntimeError(
                "Grupo aberto, mas nenhuma mensagem foi extraída. O DOM do WhatsApp pode ter mudado; ajuste seletores --wa-* ou rode novamente com o chat já visível."
            )

        return _filter_since(data, since_timestamp)

    def _open_group(self, page, group_name: str) -> None:
        page.wait_for_timeout(4000)
        search_box = page.locator(self.group_search_selector).first
        search_box.click(timeout=15000)
        search_box.fill("")
        search_box.type(group_name, delay=40)
        page.wait_for_timeout(1500)

        escaped = group_name.replace('"', '\\"')
        chat_candidate = page.locator(f'span[title="{escaped}"]').first
        if chat_candidate.count() > 0:
            chat_candidate.click(timeout=10000)
        else:
            page.get_by_text(group_name, exact=False).first.click(timeout=10000)

        page.wait_for_timeout(1500)

        header = page.locator("header span[title]").first
        if header.count() == 0:
            raise RuntimeError("Não foi possível abrir o grupo no WhatsApp Web.")


def extract_author_and_timestamp(meta_text: str) -> tuple[str, datetime]:
    if not meta_text:
        return "desconhecido", datetime.now().replace(microsecond=0)

    match = re.match(r"^\[(?P<hour>\d{1,2}:\d{2})(?:,\s*(?P<date>\d{1,2}/\d{1,2}/\d{2,4}))?\]\s*(?P<author>.*?):\s*$", meta_text)
    if not match:
        return "desconhecido", datetime.now().replace(microsecond=0)

    author = (match.group("author") or "desconhecido").strip() or "desconhecido"
    hour = match.group("hour")
    date_part = match.group("date")

    if date_part:
        day, month, year = [int(x) for x in date_part.split("/")]
        if year < 100:
            year += 2000
        hour_i, minute_i = [int(x) for x in hour.split(":")]
        timestamp = datetime(year, month, day, hour_i, minute_i)
    else:
        today = datetime.now().date()
        hour_i, minute_i = [int(x) for x in hour.split(":")]
        timestamp = datetime(today.year, today.month, today.day, hour_i, minute_i)

    return author, timestamp


_extract_author_and_timestamp = extract_author_and_timestamp


def _filter_since(messages: list[dict[str, Any]], since_timestamp: datetime | None) -> list[dict[str, Any]]:
    if since_timestamp is None:
        return messages
    return [m for m in messages if datetime.fromisoformat(m["timestamp"]) > since_timestamp]


__all__ = [
    "JsonFileCollector",
    "MessageCollector",
    "MockCollector",
    "PlaywrightWhatsAppCollector",
    "extract_author_and_timestamp",
    "_extract_author_and_timestamp",
]

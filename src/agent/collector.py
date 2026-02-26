from __future__ import annotations

import hashlib
import json
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
    """Coletor simples para dados exportados/capturados em JSON.

    Formato esperado:
    {
      "Nome do Grupo": [
        {"author": "...", "timestamp": "2026-01-01T10:00:00", "text": "...", "external_id": "..."}
      ]
    }
    """

    def __init__(self, source_path: str) -> None:
        self.source_path = Path(source_path)

    def collect_messages(self, group_name: str, since_timestamp: datetime | None = None) -> list[dict[str, Any]]:
        if not self.source_path.exists():
            raise FileNotFoundError(f"Arquivo de mensagens não encontrado: {self.source_path}")

        payload = json.loads(self.source_path.read_text(encoding="utf-8"))
        group_messages = payload.get(group_name, [])
        if not isinstance(group_messages, list):
            raise ValueError(f"Grupo '{group_name}' deve conter uma lista de mensagens no JSON.")

        return _filter_since(group_messages, since_timestamp)


class PlaywrightWhatsAppCollector(MessageCollector):
    """Coletor WhatsApp Web com Playwright (experimental).

    - Mantém sessão usando user-data-dir persistente.
    - Primeira execução requer login manual por QR.
    - Seletores do WhatsApp mudam ao longo do tempo; por isso são parametrizáveis.
    """

    def __init__(
        self,
        profile_dir: str,
        headless: bool = False,
        max_messages_visible: int = 300,
        group_search_selector: str = "div[contenteditable='true'][data-tab='3']",
        message_row_selector: str = "div[role='row']",
        author_selector: str = "[data-pre-plain-text]",
        text_selector: str = "span.selectable-text",
    ) -> None:
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self.max_messages_visible = max_messages_visible
        self.group_search_selector = group_search_selector
        self.message_row_selector = message_row_selector
        self.author_selector = author_selector
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

            page.wait_for_timeout(5000)
            search_box = page.locator(self.group_search_selector).first
            search_box.click()
            search_box.fill(group_name)
            page.wait_for_timeout(1500)

            # Tenta abrir o grupo pelo nome visível.
            page.get_by_text(group_name, exact=False).first.click(timeout=10000)
            page.wait_for_timeout(2000)

            rows = page.locator(self.message_row_selector)
            count = min(rows.count(), self.max_messages_visible)
            data: list[dict[str, Any]] = []

            for idx in range(count):
                row = rows.nth(idx)
                text = " ".join(row.locator(self.text_selector).all_inner_texts()).strip()
                meta = " ".join(row.locator(self.author_selector).all_inner_texts()).strip()
                if not text:
                    continue

                author = _extract_author_from_meta(meta)
                timestamp = datetime.now().replace(microsecond=0).isoformat()
                external_id = f"pw-{idx}-" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
                data.append(
                    {
                        "author": author,
                        "timestamp": timestamp,
                        "text": text,
                        "external_id": external_id,
                    }
                )

            context.close()

        return _filter_since(data, since_timestamp)


def _extract_author_from_meta(meta_text: str) -> str:
    if not meta_text:
        return "desconhecido"
    if "]" in meta_text:
        right = meta_text.split("]", maxsplit=1)[1].strip()
        if ":" in right:
            return right.split(":", maxsplit=1)[0].strip() or "desconhecido"
    return "desconhecido"


def _filter_since(messages: list[dict[str, Any]], since_timestamp: datetime | None) -> list[dict[str, Any]]:
    if since_timestamp is None:
        return messages
    return [m for m in messages if datetime.fromisoformat(m["timestamp"]) > since_timestamp]

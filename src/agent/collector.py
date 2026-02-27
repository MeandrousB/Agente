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
        if group_name not in payload:
            available = ", ".join(sorted(payload.keys()))
            raise ValueError(
                f"Grupo '{group_name}' não encontrado no JSON. Grupos disponíveis: {available or '(nenhum)'}"
            )

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

            self._open_group(page, group_name)

            # Extrai todas as mensagens visíveis de uma só vez via JS.
            # Isso evita timeouts de seletores individuais (WhatsApp tem
            # mensagens de sistema / separadores sem [data-pre-plain-text])
            # e é imune à re-renderização virtual durante iteração Python.
            raw_items: list[dict] = page.evaluate(
                """(maxItems) => {
                    const rows = document.querySelectorAll("div[role='row']");
                    const results = [];
                    for (const row of rows) {
                        if (results.length >= maxItems) break;
                        // meta: preferir atributo direto na row; fallback em filho
                        let meta = row.getAttribute("data-pre-plain-text");
                        if (!meta) {
                            const el = row.querySelector("[data-pre-plain-text]");
                            meta = el ? el.getAttribute("data-pre-plain-text") : null;
                        }
                        // texto: copyable-text > selectable-text > innerText
                        let spans = row.querySelectorAll("span.selectable-text.copyable-text");
                        if (!spans.length) spans = row.querySelectorAll("span.selectable-text");
                        const text = spans.length
                            ? Array.from(spans).map(s => s.innerText).join(" ").trim()
                            : (row.innerText || "").trim();
                        if (text) results.push({ meta: meta || "", text: text });
                    }
                    return results;
                }""",
                self.max_messages_visible,
            )

            context.close()

        data: list[dict[str, Any]] = []
        for item in raw_items:
            text = (item.get("text") or "").strip()
            if not text:
                continue
            author, timestamp = extract_author_and_timestamp(item.get("meta") or "")
            external_seed = f"{author}|{timestamp.isoformat()}|{text}"
            external_id = f"pw-{hashlib.sha1(external_seed.encode('utf-8')).hexdigest()[:16]}"
            data.append(
                {
                    "author": author,
                    "timestamp": timestamp.isoformat(),
                    "text": text,
                    "external_id": external_id,
                }
            )

        if not data and since_timestamp is None:
            raise RuntimeError(
                "Grupo aberto, mas nenhuma mensagem foi extraída. O DOM do WhatsApp pode ter "
                "mudado; rode novamente com o chat já visível ou reporte os seletores."
            )

        return _filter_since(data, since_timestamp)

    def _open_group(self, page: Any, group_name: str) -> None:
        # Aguarda o WhatsApp carregar (QR ou chat list)
        page.wait_for_timeout(4000)

        search_box = page.locator(self.group_search_selector).first
        if search_box.count() == 0:
            search_box = page.locator("div[contenteditable='true'][data-tab='10']").first
        if search_box.count() == 0:
            search_box = page.locator("div[contenteditable='true']").first

        search_box.click(timeout=15000)
        search_box.fill("")
        # press_sequentially é a API moderna; type() foi depreciado no Playwright >= 1.40
        try:
            search_box.press_sequentially(group_name, delay=40)
        except AttributeError:
            search_box.type(group_name, delay=40)  # type: ignore[attr-defined]
        page.wait_for_timeout(1800)

        escaped = group_name.replace('"', '\\"')
        chat_candidate = page.locator(f'span[title="{escaped}"]').first
        if chat_candidate.count() > 0:
            chat_candidate.click(timeout=10000)
        else:
            page.get_by_text(group_name, exact=False).first.click(timeout=10000)

        # Aguarda o painel de mensagens aparecer
        page.wait_for_timeout(2500)

        header = page.locator("header span[title]").first
        if header.count() == 0:
            raise RuntimeError("Não foi possível abrir o grupo no WhatsApp Web.")

        # Rola para cima para carregar mensagens mais antigas dentro do limite visível
        chat_container = page.locator("div[role='application']").first
        if chat_container.count() > 0:
            chat_container.hover()
            for _ in range(5):
                page.mouse.wheel(0, -3000)
                page.wait_for_timeout(600)
            # Volta ao final para garantir que as mensagens recentes estão visíveis
            page.mouse.wheel(0, 15000)
            page.wait_for_timeout(1000)

        # Aguarda pelo menos uma linha de mensagem estar presente
        try:
            page.locator("div[role='row']").first.wait_for(timeout=8000)
        except Exception:
            pass  # continua mesmo sem linhas – aviso será dado pelo chamador


def extract_author_and_timestamp(meta_text: str) -> tuple[str, datetime]:
    if not meta_text:
        return "desconhecido", datetime.now().replace(microsecond=0)

    match = re.match(
        r"^\[(?P<hour>\d{1,2}:\d{2})(?:,\s*(?P<date>\d{1,2}/\d{1,2}/\d{2,4}))?\]\s*(?P<author>.*?):\s*$",
        meta_text,
    )
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


__all__ = [
    "JsonFileCollector",
    "MessageCollector",
    "MockCollector",
    "PlaywrightWhatsAppCollector",
    "extract_author_and_timestamp",
    "_extract_author_and_timestamp",
]

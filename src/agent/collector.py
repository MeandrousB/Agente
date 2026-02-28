from __future__ import annotations

import hashlib
import json
import logging
import re
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# JavaScript snapshot: captura todas as linhas visíveis de uma vez, sem loops
# Python que causam timeouts de seletor individual.
_JS_SNAPSHOT = """(maxItems) => {
    const rows = document.querySelectorAll("div[role='row']");
    const results = [];
    for (const row of rows) {
        if (results.length >= maxItems) break;
        let meta = row.getAttribute("data-pre-plain-text");
        if (!meta) {
            const el = row.querySelector("[data-pre-plain-text]");
            meta = el ? el.getAttribute("data-pre-plain-text") : null;
        }
        let spans = row.querySelectorAll("span.selectable-text.copyable-text");
        if (!spans.length) spans = row.querySelectorAll("span.selectable-text");
        const text = spans.length
            ? Array.from(spans).map(s => s.innerText).join(" ").trim()
            : (row.innerText || "").trim();
        if (text) results.push({ meta: meta || "", text: text });
    }
    return results;
}"""


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
    """Coletor simples para dados exportados/capturados em JSON."""

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
    """Coletor WhatsApp Web com Playwright.

    Uso como context manager (recomendado para múltiplas buscas):
        with PlaywrightWhatsAppCollector(profile_dir=...) as c:
            msgs_a = c.collect_messages("Grupo A")
            msgs_b = c.collect_messages("Grupo B")

    Uso standalone (retrocompatível — abre/fecha o browser a cada chamada):
        c = PlaywrightWhatsAppCollector(profile_dir=...)
        msgs = c.collect_messages("Grupo A")
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

        # Estado da sessão persistente (preenchido pelo context manager)
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None

    # ── Context manager ──────────────────────────────────────────────────────

    def __enter__(self) -> "PlaywrightWhatsAppCollector":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright não está instalado. "
                "Rode: python -m pip install playwright && python -m playwright install chromium"
            ) from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().__enter__()
        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
        )
        self._page = self._context.new_page()
        self._page.goto("https://web.whatsapp.com", wait_until="domcontentloaded")
        # Aguarda o WhatsApp carregar completamente (QR ou lista de chats)
        self._page.wait_for_timeout(5000)
        logger.info("Sessão WhatsApp Web aberta (perfil: %s)", self.profile_dir)
        return self

    def __exit__(self, *_: object) -> None:
        try:
            if self._context:
                self._context.close()
        except Exception:
            pass
        try:
            if self._playwright:
                self._playwright.__exit__(None, None, None)
        except Exception:
            pass
        self._playwright = None
        self._context = None
        self._page = None
        logger.info("Sessão WhatsApp Web encerrada.")

    # ── API pública ───────────────────────────────────────────────────────────

    def collect_messages(
        self,
        group_name: str,
        since_timestamp: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Coleta mensagens do grupo.

        Se chamado dentro de um bloco `with` (context manager), reutiliza o
        browser já aberto. Caso contrário, abre e fecha o browser nesta chamada.
        """
        if self._page is not None:
            # Modo sessão: reutiliza browser já aberto
            return self._collect_from_page(self._page, group_name, since_timestamp)

        # Modo standalone: abre/fecha browser por chamada
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright não está instalado. "
                "Rode: python -m pip install playwright && python -m playwright install chromium"
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
            try:
                result = self._collect_from_page(page, group_name, since_timestamp)
            finally:
                context.close()
        return result

    # ── Internos ──────────────────────────────────────────────────────────────

    def _collect_from_page(
        self,
        page: Any,
        group_name: str,
        since_timestamp: datetime | None,
    ) -> list[dict[str, Any]]:
        """Abre o grupo na página já carregada e extrai as mensagens."""
        self._open_group(page, group_name)

        # Tenta extrair mensagens com até 3 tentativas para lidar com
        # virtualização do DOM que pode demorar a renderizar após o scroll.
        raw_items: list[dict] = []
        for attempt in range(3):
            raw_items = page.evaluate(_JS_SNAPSHOT, self.max_messages_visible)
            if raw_items:
                break
            logger.debug(
                "  Tentativa %d: nenhuma linha em '%s', aguardando...",
                attempt + 1,
                group_name,
            )
            page.wait_for_timeout(2000)

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

        if not data:
            logger.warning("Grupo '%s' aberto mas sem mensagens extraídas.", group_name)
            # Retorna lista vazia — quem chama decide o que fazer.
            # NÃO lança exceção para não mascarar "grupo encontrado mas vazio"
            # como "grupo errado" no loop de tentativas do pipeline.
            return []

        return _filter_since(data, since_timestamp)

    def _open_group(self, page: Any, group_name: str) -> None:
        """Navega até o grupo no WhatsApp Web.

        Raises:
            RuntimeError: se o grupo não for encontrado nos resultados de busca
                          ou se o chat não abrir dentro do timeout.
        """
        # Reseta busca anterior e garante foco na sidebar
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)
        except Exception:
            pass

        # Localiza a caixa de busca com múltiplos seletores de fallback
        search_box = None
        for sel in [
            self.group_search_selector,                          # data-tab='3'
            "div[contenteditable='true'][data-tab='10']",
            "[data-testid='search-input']",
            "div[contenteditable='true'][placeholder]",
        ]:
            loc = page.locator(sel).first
            try:
                if loc.is_visible(timeout=1000):
                    search_box = loc
                    break
            except Exception:
                continue

        if search_box is None:
            # Tenta clicar no ícone de lupa para abrir o campo de busca
            try:
                page.locator("span[data-icon='search']").first.click(timeout=5000)
                page.wait_for_timeout(600)
                search_box = page.locator(self.group_search_selector).first
            except Exception:
                search_box = page.locator("div[contenteditable='true']").first

        search_box.click(timeout=15000)
        page.wait_for_timeout(400)

        # Limpa campo e digita o nome do grupo
        try:
            search_box.fill("")
        except Exception:
            pass
        page.wait_for_timeout(200)

        try:
            search_box.press_sequentially(group_name, delay=40)
        except AttributeError:
            search_box.type(group_name, delay=40)  # type: ignore[attr-defined]

        # Aguarda resultados aparecerem — usa wait_for_selector para evitar
        # race condition com contagem imediata via .count()
        escaped = group_name.replace("\\", "\\\\").replace('"', '\\"')
        first_word = group_name.split()[0].replace("\\", "\\\\").replace('"', '\\"')

        found_selector: str | None = None
        try:
            page.wait_for_selector(f'span[title="{escaped}"]', timeout=5000)
            found_selector = f'span[title="{escaped}"]'
            logger.debug("  Resultado exato encontrado para '%s'", group_name)
        except Exception:
            # Tenta correspondência parcial pela primeira palavra
            try:
                page.wait_for_selector(f'span[title*="{first_word}"]', timeout=3000)
                found_selector = f'span[title*="{first_word}"]'
                logger.debug("  Resultado parcial encontrado para '%s'", group_name)
            except Exception:
                raise RuntimeError(
                    f"Grupo '{group_name}' não encontrado no WhatsApp "
                    f"(nenhum resultado após 8 s de busca)."
                )

        page.locator(found_selector).first.click(timeout=10000)

        # Aguarda o painel do chat abrir (header com título visível)
        try:
            page.wait_for_selector("header span[title]", timeout=12000)
        except Exception:
            raise RuntimeError(
                f"Chat '{group_name}' clicado mas painel não abriu dentro do timeout."
            )

        # Aguarda pelo menos uma linha de mensagem estar presente
        try:
            page.locator("div[role='row']").first.wait_for(timeout=10000)
            page.wait_for_timeout(1200)  # estabilização após render inicial
        except Exception:
            pass  # grupo pode estar vazio; aviso virá do chamador

        # Rola para cima para capturar contexto histórico, depois volta ao fim
        chat_container = page.locator("div[role='application']").first
        try:
            if chat_container.is_visible(timeout=2000):
                chat_container.hover()
                for _ in range(5):
                    page.mouse.wheel(0, -3000)
                    page.wait_for_timeout(600)
                page.mouse.wheel(0, 15000)
                page.wait_for_timeout(1500)
                # Aguarda mensagens recentes re-renderizarem após scroll de volta
                try:
                    page.locator("div[role='row']").first.wait_for(timeout=5000)
                except Exception:
                    pass
        except Exception:
            pass


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

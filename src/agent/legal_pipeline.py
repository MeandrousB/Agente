"""
legal_pipeline.py
-----------------
Pipeline completo: Gestão Jurídico → WhatsApp → LLM → Timeline.

Fluxo por caso:
  1. Busca caso no JuridicoTamaras (via TamarasClient)
  2. Lê último comentário da timeline (para evitar redundância)
  3. Tenta localizar o(s) grupo(s) de WhatsApp (até 4 variações)
  4. Coleta mensagens (rola para cima para captar contexto)
  5. Gera comentário estruturado com LLM (Ollama)
  6. Posta na timeline e verifica
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .llm_summarizer import _post_json
from .tamaras_client import TamarasClient

logger = logging.getLogger(__name__)

# ── Template do comentário ────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "Você é assistente jurídico especializado em transações imobiliárias no Brasil. "
    "Analisa conversas de grupos de WhatsApp e produz comentários objetivos para a "
    "timeline de casos. Retorna APENAS o comentário formatado, sem explicações adicionais."
)

_USER_PROMPT_TEMPLATE = """\
Você está analisando o caso: {case_id} — {property_address}

PARTES DO CASO:
{parties}

ÚLTIMO COMENTÁRIO JÁ NA TIMELINE (não seja redundante):
---
{last_comment}
---

MENSAGENS DOS GRUPOS DE WHATSAPP ({msg_count} mensagens):
{messages_text}

Gere um comentário para a timeline seguindo EXATAMENTE este template,
sem omitir nenhuma seção e sem alterar os rótulos:

Status atual: [o que está acontecendo agora, objetivamente; itens que impedem avanço, pendências existentes]

Último encaminhamento: [até 3 linhas com o último encaminhamento real, com data/hora se disponível]

Próximo passo: [ação próxima concreta, até 3 linhas]

Pendências (bulletpoints):
·  [pendência 1]
·  [pendência 2]

REGRAS OBRIGATÓRIAS:
1. Não invente nada. Se não houver informação suficiente, diga explicitamente.
2. Evite frases vagas ("estão alinhando", "em andamento"). Seja específico.
3. Cite nomes, datas e valores quando disponíveis nas mensagens.
4. Não repita o que já está no último comentário existente.
5. Se não houver pendências claras, escreva: ·  Nenhuma pendência identificada neste ciclo.\
"""


# ── Resultado por caso ────────────────────────────────────────────────────────

@dataclass
class CaseResult:
    case_id: str
    property_address: str
    groups_searched: list[str] = field(default_factory=list)
    groups_found: list[str] = field(default_factory=list)
    message_count: int = 0
    generated_comment: str = ""
    posted_comment_id: str = ""
    verified: bool = False
    error: str = ""
    skipped: bool = False
    skip_reason: str = ""

    @property
    def success(self) -> bool:
        return bool(self.posted_comment_id) and self.verified and not self.error

    def summary_line(self) -> str:
        if self.skipped:
            return f"⏭ Pulado — {self.skip_reason}"
        if self.error:
            return f"❌ Erro — {self.error[:120]}"
        if self.success:
            return f"✅ Comentário postado (id={self.posted_comment_id[:8]}…, msgs={self.message_count})"
        return "⚠ Incompleto"


# ── Helpers de busca ──────────────────────────────────────────────────────────

def _build_search_terms(case: dict) -> list[str]:
    """Gera termos de busca ordenados por especificidade para localizar o grupo."""
    addr: str = case.get("property_address") or ""
    parties: list[dict] = case.get("parties") or []

    terms: list[str] = []

    # 1. Rua + número (ex.: "Bartira 901", "Augusta 1122")
    m = re.match(
        r"(?:Rua|Av\.?|Avenida|Alameda|R\.)\s+([A-Za-zÀ-ú\s]+?),?\s*(\d+)",
        addr,
        re.IGNORECASE,
    )
    if m:
        street = m.group(1).strip()
        number = m.group(2).strip()
        terms.append(f"{street} {number}")  # "Bartira 901"
        terms.append(street)                # "Bartira"
    else:
        # Fallback genérico: primeiras 2 palavras
        words = addr.split()
        if len(words) >= 2:
            terms.append(" ".join(words[:2]))
        if words:
            terms.append(words[0])

    # 2. Apelido do cliente (último token após vírgula, ex.: "Ana", "KUMPERA")
    parts = [p.strip() for p in addr.split(",")]
    if len(parts) >= 2:
        nickname = parts[-1].strip().strip("()")
        if nickname and not nickname.isdigit():
            terms.append(nickname)

    # 3. Primeiro nome de cada parte (vendedor e comprador)
    for p in parties:
        full = (p.get("name") or "").strip()
        if not full:
            continue
        first = full.split()[0]
        last = full.split()[-1] if len(full.split()) > 1 else ""
        for token in (first, last):
            if token and token not in terms:
                terms.append(token)

    # 4. Endereço completo como último recurso
    full_addr = addr.split(",")[0].strip()
    if full_addr not in terms:
        terms.append(full_addr)

    # Deduplicar preservando ordem, ignorar case
    seen: set[str] = set()
    unique: list[str] = []
    for t in terms:
        key = t.lower()
        if t and key not in seen:
            seen.add(key)
            unique.append(t)
    return unique


def _collect_group(
    collector: Any,
    search_terms: list[str],
    max_attempts: int = 4,
) -> tuple[list[dict], str]:
    """Tenta até max_attempts termos; retorna (mensagens, nome_do_grupo)."""
    for term in search_terms[:max_attempts]:
        try:
            logger.info("  Buscando grupo WhatsApp: '%s'", term)
            msgs = collector.collect_messages(group_name=term)
            if msgs:
                logger.info("  ✓ Grupo encontrado: '%s' (%d mensagens)", term, len(msgs))
                return msgs, term
        except RuntimeError as exc:
            # "Nenhuma mensagem" não é erro fatal — grupo errado
            if "nenhuma mensagem" in str(exc).lower():
                logger.debug("  Grupo '%s' aberto mas vazio; tentando próximo.", term)
            else:
                logger.debug("  Termo '%s' falhou: %s", term, exc)
        except Exception as exc:
            logger.debug("  Termo '%s' falhou: %s", term, exc)
    return [], ""


# ── Geração do comentário via LLM ─────────────────────────────────────────────

def _generate_comment(
    case: dict,
    messages: list[dict],
    last_comment: str,
    model: str,
    ollama_url: str,
    timeout_s: int,
) -> str:
    parties_text = "\n".join(
        f"  - {p.get('party_type', '?').upper()}: {p.get('name', '?')}"
        for p in (case.get("parties") or [])
    ) or "  (sem partes cadastradas)"

    lines: list[str] = []
    for msg in sorted(messages, key=lambda x: x.get("timestamp", "")):
        ts = (msg.get("timestamp") or "")[:16].replace("T", " ")
        author = msg.get("author") or "?"
        text = msg.get("text") or ""
        lines.append(f"[{ts}] {author}: {text}")
    messages_text = "\n".join(lines) if lines else "(nenhuma mensagem coletada)"

    prompt = _USER_PROMPT_TEMPLATE.format(
        case_id=case.get("id", ""),
        property_address=case.get("property_address", ""),
        parties=parties_text,
        last_comment=last_comment.strip() if last_comment else "(nenhum comentário anterior)",
        msg_count=len(messages),
        messages_text=messages_text,
    )

    payload = {
        "model": model,
        "system": _SYSTEM_PROMPT,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.1},
    }
    data = _post_json(
        f"{ollama_url.rstrip('/')}/api/generate",
        payload,
        timeout_s=timeout_s,
    )
    return str(data.get("response") or "").strip()


# ── Pipeline principal ────────────────────────────────────────────────────────

class LegalCasePipeline:
    """
    Orquestra o processamento de todos os casos de Gestão Jurídico:
      - Coleta dados do JuridicoTamaras
      - Busca grupos de WhatsApp (vendedor + comprador)
      - Gera comentário estruturado via LLM
      - Posta na timeline e valida
    """

    def __init__(
        self,
        tamaras_client: TamarasClient,
        wa_profile_dir: str = ".wa_profile",
        wa_headless: bool = False,
        wa_max_messages: int = 500,
        ollama_model: str = "qwen3:4b",
        ollama_url: str = "http://localhost:11434",
        llm_timeout_s: int = 240,
    ) -> None:
        self.tamaras = tamaras_client
        self.wa_profile_dir = wa_profile_dir
        self.wa_headless = wa_headless
        self.wa_max_messages = wa_max_messages
        self.ollama_model = ollama_model
        self.ollama_url = ollama_url
        self.llm_timeout_s = llm_timeout_s

    def run(
        self,
        progress_cb: Callable[[int, int, str, str], None] | None = None,
    ) -> list[CaseResult]:
        """Processa todos os casos de Gestão Jurídico. Retorna resultado por caso."""
        cases = self.tamaras.get_gestao_juridico_cases()
        results: list[CaseResult] = []

        for idx, case in enumerate(cases):
            case_id = case.get("id", f"caso-{idx}")
            addr = case.get("property_address", case_id)
            logger.info("═══ Processando %s (%d/%d) ═══", case_id, idx + 1, len(cases))

            if progress_cb:
                progress_cb(idx, len(cases), case_id, "iniciando")

            result = CaseResult(case_id=case_id, property_address=addr)
            try:
                result = self._process_case(
                    case,
                    cb=lambda step, _id=case_id, _i=idx, _n=len(cases): (
                        progress_cb(_i, _n, _id, step) if progress_cb else None
                    ),
                )
            except Exception as exc:
                logger.exception("Erro inesperado no caso %s.", case_id)
                result.error = str(exc)

            results.append(result)

        return results

    def _process_case(
        self,
        case: dict,
        cb: Callable[[str], None] | None = None,
    ) -> CaseResult:
        case_id = case["id"]
        addr = case.get("property_address", case_id)
        result = CaseResult(case_id=case_id, property_address=addr)

        def _cb(step: str) -> None:
            logger.info("  [%s] %s", case_id, step)
            if cb:
                cb(step)

        # ── 1. Último comentário existente ────────────────────────────────────
        _cb("lendo último comentário da timeline")
        comments = self.tamaras.get_recent_comments(case_id, limit=1)
        last_comment = ""
        if comments and isinstance(comments[0], dict):
            last_comment = comments[0].get("comment") or ""

        # ── 2. Termos de busca para o WhatsApp ───────────────────────────────
        search_terms = _build_search_terms(case)
        result.groups_searched = search_terms[:4]
        logger.info("  Termos de busca: %s", search_terms[:4])

        # ── 3. Coletar mensagens do WhatsApp ──────────────────────────────────
        _cb("buscando grupo no WhatsApp")

        # Import aqui para evitar import circular e isolar o loop asyncio
        from .collector import PlaywrightWhatsAppCollector

        collector = PlaywrightWhatsAppCollector(
            profile_dir=self.wa_profile_dir,
            headless=self.wa_headless,
            max_messages_visible=self.wa_max_messages,
        )

        all_messages: list[dict] = []

        # Busca pelo grupo principal (vendedor ou genérico)
        msgs, group_found = _collect_group(collector, search_terms, max_attempts=4)
        if not msgs:
            result.skipped = True
            result.skip_reason = (
                f"Grupo não encontrado após 4 tentativas. "
                f"Termos usados: {search_terms[:4]}"
            )
            logger.warning("  Caso %s pulado: %s", case_id, result.skip_reason)
            return result

        result.groups_found.append(group_found)
        all_messages.extend(msgs)

        # Tenta também buscar grupo do comprador (sufixo "(comprador)" / "comprador")
        buyer_terms = [f"{t} comprador" for t in search_terms[:2]] + [
            f"{t} (comprador)" for t in search_terms[:2]
        ]
        buyer_msgs, buyer_group = _collect_group(collector, buyer_terms, max_attempts=4)
        if buyer_msgs and buyer_group != group_found:
            _cb(f"grupo do comprador encontrado: {buyer_group}")
            result.groups_found.append(buyer_group)
            all_messages.extend(buyer_msgs)

        result.message_count = len(all_messages)
        _cb(f"{result.message_count} mensagem(ns) coletada(s) de {len(result.groups_found)} grupo(s)")

        # ── 4. Gerar comentário com LLM ───────────────────────────────────────
        _cb("gerando comentário com LLM")
        generated = _generate_comment(
            case=case,
            messages=all_messages,
            last_comment=last_comment,
            model=self.ollama_model,
            ollama_url=self.ollama_url,
            timeout_s=self.llm_timeout_s,
        )

        if not generated:
            result.error = "LLM retornou resposta vazia."
            return result

        result.generated_comment = generated

        # ── 5. Postar na timeline ─────────────────────────────────────────────
        _cb("postando comentário na timeline")
        posted = self.tamaras.post_comment(case_id, generated)
        comment_id: str = (posted.get("id") or "") if isinstance(posted, dict) else ""

        if not comment_id:
            result.error = f"POST sem ID retornado: {str(posted)[:200]}"
            return result

        result.posted_comment_id = comment_id

        # ── 6. Verificar ──────────────────────────────────────────────────────
        _cb("verificando comentário salvo")
        result.verified = self.tamaras.verify_comment(case_id, comment_id)
        if not result.verified:
            result.error = f"Comentário {comment_id[:8]}… não encontrado após POST."

        return result

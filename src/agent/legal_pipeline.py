"""
legal_pipeline.py
-----------------
Pipeline completo: Gestão Jurídico → WhatsApp → LLM → Timeline.

Fluxo por caso:
  1. Busca caso no JuridicoTamaras (via TamarasClient)
  2. Lê último comentário da timeline (para evitar redundância)
  3. Abre o browser UMA vez e busca o grupo de vendedores e compradores
  4. Coleta mensagens de ambos os grupos (contexto das últimas trocas)
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

MENSAGENS DOS GRUPOS DE WHATSAPP ({msg_count} mensagens de {group_count} grupo(s)):
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


# ── Construção dos termos de busca ────────────────────────────────────────────

def _build_search_terms(
    case: dict,
) -> tuple[list[str], list[str], list[str]]:
    """Retorna (addr_terms, seller_name_terms, buyer_name_terms).

    addr_terms          — variações do endereço, usadas como primeiro recurso
                          para ambos os grupos (vendedor e comprador).
    seller_name_terms   — nomes das partes vendedoras, fallback se addr falhar.
    buyer_name_terms    — nomes das partes compradoras, fallback para grupo do
                          comprador.
    """
    addr: str = case.get("property_address") or ""
    parties: list[dict] = case.get("parties") or []

    def _dedup(lst: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for t in lst:
            key = t.lower()
            if t and key not in seen:
                seen.add(key)
                out.append(t)
        return out

    # ── Termos de endereço ────────────────────────────────────────────────────
    addr_terms: list[str] = []

    m = re.match(
        r"(?:Rua|Av\.?|Avenida|Alameda|R\.)\s+([A-Za-zÀ-ú\s]+?),?\s*(\d+)",
        addr,
        re.IGNORECASE,
    )
    if m:
        street = m.group(1).strip()
        number = m.group(2).strip()
        addr_terms.append(f"{street} {number}")   # "Bartira 901"
        addr_terms.append(street)                  # "Bartira"
    else:
        words = addr.split()
        if len(words) >= 2:
            addr_terms.append(" ".join(words[:2]))
        if words:
            addr_terms.append(words[0])

    # Apelido após vírgula no endereço (ex.: "Ana", "KUMPERA")
    addr_parts = [p.strip() for p in addr.split(",")]
    if len(addr_parts) >= 2:
        nickname = addr_parts[-1].strip().strip("()")
        if nickname and not nickname.isdigit() and nickname not in addr_terms:
            addr_terms.append(nickname)

    full_addr = addr.split(",")[0].strip()
    if full_addr and full_addr not in addr_terms:
        addr_terms.append(full_addr)

    # ── Nomes por lado (vendedor / comprador) ─────────────────────────────────
    _BUYER_TYPES  = {"comprador", "compradora", "buyer"}
    _SELLER_TYPES = {"vendedor", "vendedora", "seller"}

    seller_name_terms: list[str] = []
    buyer_name_terms:  list[str] = []

    for p in parties:
        full = (p.get("name") or "").strip()
        if not full:
            continue
        ptype = (p.get("party_type") or "").strip().lower()
        tokens = full.split()
        first = tokens[0] if tokens else ""
        last  = tokens[-1] if len(tokens) > 1 else ""
        names = [t for t in (first, last) if t and len(t) > 2 and t.lower() not in {a.lower() for a in addr_terms}]

        if ptype in _BUYER_TYPES:
            for n in names:
                if n not in buyer_name_terms:
                    buyer_name_terms.append(n)
        else:
            # vendedor ou tipo desconhecido → lado do vendedor
            for n in names:
                if n not in seller_name_terms:
                    seller_name_terms.append(n)

    return _dedup(addr_terms), _dedup(seller_name_terms), _dedup(buyer_name_terms)


# ── Validação de relevância ───────────────────────────────────────────────────

def _is_relevant_to_case(messages: list[dict], case: dict) -> bool:
    """Verifica se as mensagens parecem ser sobre este caso específico.

    Usado para validar grupos encontrados por nome de parte (evitar alimentar
    o LLM com contexto de um caso diferente).
    """
    if not messages:
        return False

    addr = (case.get("property_address") or "").lower()
    # Palavras significativas do endereço (ignora preposições e números curtos)
    key_words = [
        w for w in re.findall(r"[a-záàâãéêíóôõúç]+", addr, re.IGNORECASE)
        if len(w) > 4
    ]

    all_text = " ".join((m.get("text") or "").lower() for m in messages[:40])

    if any(word in all_text for word in key_words[:3]):
        return True

    case_id = (case.get("id") or "").lower()
    if case_id and case_id in all_text:
        return True

    return False


# ── Coleta de grupo ───────────────────────────────────────────────────────────

def _collect_group(
    collector: Any,
    addr_terms: list[str],
    name_terms: list[str] | None = None,
    case: dict | None = None,
    label: str = "grupo",
) -> tuple[list[dict], str]:
    """Localiza e coleta mensagens de um grupo WhatsApp.

    Estratégia:
      1. Tenta cada termo de endereço. Na primeira abertura bem-sucedida,
         para imediatamente (mesmo que vazio) — evita buscar pelo nome de
         parte quando o endereço já identificou o grupo correto.
      2. Só tenta nomes de partes se TODOS os termos de endereço falharem
         (i.e., grupo não encontrado no WhatsApp).
      3. Correspondências por nome de parte são validadas contra o endereço
         do caso para evitar alimentar o LLM com contexto errado.

    Retorna:
      (mensagens, nome_do_termo_que_abriu_o_grupo)
      Se o grupo não for encontrado: ([], "")
    """
    # Fase 1: termos de endereço
    for term in addr_terms:
        try:
            logger.info("  [%s] Buscando por endereço: '%s'", label, term)
            msgs = collector.collect_messages(group_name=term)
            # Grupo identificado pelo endereço — usa mesmo que vazio
            logger.info(
                "  [%s] ✓ Grupo '%s' aberto (%d mensagens)", label, term, len(msgs)
            )
            return msgs, term
        except RuntimeError as exc:
            err = str(exc).lower()
            if "não encontrado" in err or "nenhum resultado" in err:
                logger.debug("  [%s] '%s': não encontrado, tentando próximo", label, term)
            else:
                logger.debug("  [%s] '%s': erro — %s", label, term, exc)
        except Exception as exc:
            logger.debug("  [%s] '%s': erro — %s", label, term, exc)

    if not name_terms:
        return [], ""

    # Fase 2: termos de nome (só se endereço falhou)
    logger.info("  [%s] Endereço não localizou grupo; tentando nomes de partes.", label)
    for term in name_terms:
        try:
            logger.info("  [%s] Buscando por nome: '%s'", label, term)
            msgs = collector.collect_messages(group_name=term)
            if msgs:
                if case and not _is_relevant_to_case(msgs, case):
                    logger.warning(
                        "  [%s] Grupo '%s' encontrado por nome, mas contexto não "
                        "parece relacionado ao caso — descartado.",
                        label,
                        term,
                    )
                    continue
                logger.info("  [%s] ✓ Grupo '%s' por nome (%d msgs)", label, term, len(msgs))
                return msgs, term
        except RuntimeError as exc:
            if "não encontrado" in str(exc).lower():
                continue
            logger.debug("  [%s] '%s': erro — %s", label, term, exc)
        except Exception as exc:
            logger.debug("  [%s] '%s': erro — %s", label, term, exc)

    return [], ""


# ── Geração do comentário via LLM ─────────────────────────────────────────────

def _generate_comment(
    case: dict,
    messages: list[dict],
    groups_found: list[str],
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
    messages_text = "\n".join(lines) if lines else "(nenhuma mensagem coletada nos grupos)"

    prompt = _USER_PROMPT_TEMPLATE.format(
        case_id=case.get("id", ""),
        property_address=case.get("property_address", ""),
        parties=parties_text,
        last_comment=last_comment.strip() if last_comment else "(nenhum comentário anterior)",
        msg_count=len(messages),
        group_count=len(groups_found),
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
      - Busca grupos de WhatsApp (vendedor + comprador) em sessão única por caso
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
        """Processa todos os casos de Gestão Jurídico. Retorna resultado por caso.

        O browser do WhatsApp é aberto UMA única vez para todos os casos, evitando
        o erro "Sync API inside asyncio loop" que ocorre ao reabrir Playwright no
        mesmo thread após o primeiro contexto ter sido fechado.
        """
        from .collector import PlaywrightWhatsAppCollector

        cases = self.tamaras.get_gestao_juridico_cases()
        results: list[CaseResult] = []

        # Abre o WhatsApp Web uma única vez para toda a sessão de processamento
        with PlaywrightWhatsAppCollector(
            profile_dir=self.wa_profile_dir,
            headless=self.wa_headless,
            max_messages_visible=self.wa_max_messages,
        ) as collector:
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
                        collector=collector,
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
        collector: Any,
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

        # ── 2. Termos de busca separados por lado ─────────────────────────────
        addr_terms, seller_name_terms, buyer_name_terms = _build_search_terms(case)
        result.groups_searched = addr_terms + seller_name_terms + buyer_name_terms
        logger.info(
            "  Endereço: %s | Vendedor nomes: %s | Comprador nomes: %s",
            addr_terms, seller_name_terms, buyer_name_terms,
        )

        # ── 3. Coletar mensagens (reusa o browser já aberto pelo run()) ────────
        all_messages: list[dict] = []

        # ── Grupo do vendedor ─────────────────────────────────────────────────
        _cb("buscando grupo do vendedor")
        msgs, group_found = _collect_group(
            collector,
            addr_terms=addr_terms,
            name_terms=seller_name_terms,
            case=case,
            label="vendedor",
        )

        if not group_found:
            result.skipped = True
            result.skip_reason = (
                f"Grupo não localizado no WhatsApp. "
                f"Termos tentados: {(addr_terms + seller_name_terms)[:6]}"
            )
            logger.warning("  Caso %s pulado: %s", case_id, result.skip_reason)
            return result

        result.groups_found.append(group_found)
        all_messages.extend(msgs)
        _cb(f"grupo vendedor '{group_found}' ({len(msgs)} msgs)")

        # ── Grupo do comprador ────────────────────────────────────────────────
        # Constrói variações comuns de nomenclatura para o grupo do comprador:
        # sufixos "comprador", "C", "- C", "(comprador)" combinados com o
        # endereço; e nomes próprios dos compradores como último recurso.
        _cb("buscando grupo do comprador")
        buyer_addr_terms: list[str] = []
        for t in addr_terms[:2]:
            buyer_addr_terms.extend([
                f"{t} comprador",
                f"{t} (comprador)",
                f"{t} - comprador",
                f"{t} - C",
                f"{t} C",
            ])
        # Nomes dos compradores combinados com o principal termo de endereço
        if addr_terms:
            base = addr_terms[0]
            for n in buyer_name_terms[:2]:
                buyer_addr_terms.append(f"{base} {n}")
        # Nomes dos compradores sozinhos (fallback validado por _is_relevant_to_case)
        buyer_name_fallback = buyer_name_terms

        buyer_msgs, buyer_group = _collect_group(
            collector,
            addr_terms=buyer_addr_terms,
            name_terms=buyer_name_fallback,
            case=case,
            label="comprador",
        )
        if buyer_msgs and buyer_group and buyer_group != group_found:
            _cb(f"grupo comprador '{buyer_group}' ({len(buyer_msgs)} msgs)")
            result.groups_found.append(buyer_group)
            all_messages.extend(buyer_msgs)
        elif not buyer_group:
            _cb("grupo do comprador não encontrado — continuando só com vendedor")

        result.message_count = len(all_messages)
        _cb(
            f"{result.message_count} mensagem(ns) de {len(result.groups_found)} grupo(s): "
            f"{result.groups_found}"
        )

        # ── 4. Gerar comentário com LLM ───────────────────────────────────────
        _cb("gerando comentário com LLM")
        generated = _generate_comment(
            case=case,
            messages=all_messages,
            groups_found=result.groups_found,
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

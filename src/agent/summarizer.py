from __future__ import annotations

import re
import unicodedata

from .models import NormalizedMessage, SummaryResult, SummaryState


class IncrementalSummarizer:
    """Resumidor simples e barato.

    Em produção, troque por integração com Ollama/API barata usando o mesmo contrato.
    """

    def summarize(self, messages: list[NormalizedMessage], state: SummaryState) -> SummaryResult:
        if not messages:
            return SummaryResult("Sem novas mensagens para resumir.", state, 0)

        decisions = list(state.decisions)
        pending = list(state.pending)
        risks = list(state.risks)

        timeline: list[str] = []
        for msg in messages:
            timeline.append(f"- [{msg.timestamp.strftime('%H:%M')}] {msg.author}: {msg.text}")
            low = _normalize_text(msg.text)
            if "decisao" in low:
                decisions.append(msg.text)
            if _contains_pending_marker(low):
                pending.append(msg.text)
            if "risco" in low:
                risks.append(msg.text)

        new_state = SummaryState(
            decisions=_dedupe_keep_order(decisions),
            pending=_dedupe_keep_order(pending),
            risks=_dedupe_keep_order(risks),
            current_status=_build_status(messages),
        )

        summary_text = (
            "## Micro-resumo\n"
            + "\n".join(timeline)
            + "\n\n## Estado atual\n"
            + f"- Decisões: {len(new_state.decisions)}\n"
            + f"- Pendências: {len(new_state.pending)}\n"
            + f"- Riscos: {len(new_state.risks)}\n"
            + f"- Status: {new_state.current_status}"
        )

        return SummaryResult(summary_text=summary_text, state=new_state, message_count=len(messages))


def _contains_pending_marker(text: str) -> bool:
    return bool(re.search(r"\bpendente(s)?\b|\bpendencia(s)?\b", text))


def _normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    no_accents = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return no_accents.lower()


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        norm = item.strip()
        if norm and norm not in seen:
            seen.add(norm)
            output.append(norm)
    return output


def _build_status(messages: list[NormalizedMessage]) -> str:
    last = messages[-1]
    return f"Última atualização de {last.author} às {last.timestamp.strftime('%H:%M')}"

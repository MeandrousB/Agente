from __future__ import annotations

import json
import logging
import os
import re
import urllib.request

from .models import NormalizedMessage, SummaryResult, SummaryState
from .summarizer import IncrementalSummarizer

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "Você é um assistente especializado em análise de conversas de grupos de trabalho em português. "
    "Extrai decisões, pendências e riscos com precisão e retorna sempre JSON válido."
)


class LLMIncrementalSummarizer:
    """Resumidor com LLM (Ollama ou OpenAI-compatible) e fallback heurístico."""

    def __init__(
        self,
        provider: str,
        model: str,
        ollama_url: str = "http://localhost:11434",
        openai_base_url: str = "https://api.openai.com",
        openai_api_key_env: str = "OPENAI_API_KEY",
        timeout_s: int = 120,
    ) -> None:
        self.provider = provider
        self.model = model
        self.ollama_url = ollama_url.rstrip("/")
        self.openai_base_url = openai_base_url.rstrip("/")
        self.openai_api_key_env = openai_api_key_env
        self.timeout_s = timeout_s
        self._fallback = IncrementalSummarizer()

    def summarize(self, messages: list[NormalizedMessage], state: SummaryState) -> SummaryResult:
        if not messages:
            return SummaryResult("Sem novas mensagens para resumir.", state, 0)

        try:
            raw_response = self._call_llm(messages, state)
            parsed = _parse_llm_response(raw_response)
            new_state = SummaryState(
                decisions=_merge_unique(state.decisions, parsed.get("decisoes", [])),
                pending=_merge_unique(state.pending, parsed.get("pendencias", [])),
                risks=_merge_unique(state.risks, parsed.get("riscos", [])),
                current_status=parsed.get("status") or _fallback_status(messages),
            )
            summary_text = parsed.get("resumo") or raw_response
            return SummaryResult(summary_text=summary_text, state=new_state, message_count=len(messages))
        except Exception as exc:
            logger.warning("LLM falhou (%s), usando resumidor heurístico.", exc)
            return self._fallback.summarize(messages, state)

    def _call_llm(self, messages: list[NormalizedMessage], state: SummaryState) -> str:
        prompt = _build_prompt(messages, state)

        if self.provider == "ollama":
            payload = {
                "model": self.model,
                "system": _SYSTEM_PROMPT,
                "prompt": prompt,
                "stream": False,
            }
            data = _post_json(f"{self.ollama_url}/api/generate", payload, timeout_s=self.timeout_s)
            return str(data.get("response", "")).strip() or "Sem conteúdo retornado pelo LLM."

        api_key = os.getenv(self.openai_api_key_env)
        if not api_key:
            raise RuntimeError(f"Variável de ambiente {self.openai_api_key_env} não definida.")

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        }
        data = _post_json(
            f"{self.openai_base_url}/v1/chat/completions",
            payload,
            timeout_s=self.timeout_s,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        choices = data.get("choices", [])
        if not choices:
            return "Sem conteúdo retornado pelo LLM."
        return str(choices[0]["message"]["content"]).strip()


def _build_prompt(messages: list[NormalizedMessage], state: SummaryState) -> str:
    state_summary = (
        f"Decisões já registradas: {state.decisions or '(nenhuma)'}\n"
        f"Pendências já registradas: {state.pending or '(nenhuma)'}\n"
        f"Riscos já registrados: {state.risks or '(nenhum)'}\n"
        f"Status anterior: {state.current_status}"
    )
    timeline = "\n".join(
        f"[{m.timestamp.strftime('%d/%m %H:%M')}] {m.author}: {m.text}"
        for m in messages
    )
    return (
        f"Estado anterior do grupo:\n{state_summary}\n\n"
        f"Novas mensagens ({len(messages)}):\n{timeline}\n\n"
        "Analise as mensagens e retorne APENAS um objeto JSON válido, sem texto extra nem markdown. Formato:\n"
        '{\n'
        '  "resumo": "micro-resumo em markdown das mensagens mais relevantes",\n'
        '  "decisoes": ["novas decisões tomadas (não repetir as já registradas)"],\n'
        '  "pendencias": ["novas pendências identificadas (não repetir as já registradas)"],\n'
        '  "riscos": ["novos riscos mencionados (não repetir os já registrados)"],\n'
        '  "status": "frase curta descrevendo o status atual"\n'
        '}'
    )


def _parse_llm_response(raw: str) -> dict:
    """Extrai JSON do retorno do LLM (pode vir com markdown, texto extra, etc.)."""
    # 1. Parse direto
    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError:
        pass

    # 2. Bloco ```json ... ```
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # 3. Qualquer { ... } no texto
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    # 4. Sem JSON: usa texto como resumo, sem atualizar estado
    return {"resumo": raw}


def _merge_unique(existing: list[str], new_items: list[str]) -> list[str]:
    """Combina listas sem duplicar itens, mantendo a ordem."""
    seen = set(existing)
    result = list(existing)
    for item in (new_items or []):
        item = item.strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _fallback_status(messages: list[NormalizedMessage]) -> str:
    last = messages[-1]
    return f"Última atualização de {last.author} às {last.timestamp.strftime('%H:%M')}"


def _post_json(url: str, payload: dict, timeout_s: int, headers: dict[str, str] | None = None) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url=url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw)

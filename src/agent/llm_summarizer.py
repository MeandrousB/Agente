from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import asdict

from .models import NormalizedMessage, SummaryResult, SummaryState
from .summarizer import IncrementalSummarizer


class LLMIncrementalSummarizer:
    """Resumidor com LLM (Ollama ou OpenAI-compatible) e fallback heurístico."""

    def __init__(
        self,
        provider: str,
        model: str,
        ollama_url: str = "http://localhost:11434",
        openai_base_url: str = "https://api.openai.com",
        openai_api_key_env: str = "OPENAI_API_KEY",
        timeout_s: int = 60,
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
            llm_text = self._call_llm(messages, state)
        except Exception:
            return self._fallback.summarize(messages, state)

        merged = self._fallback.summarize(messages, state)
        return SummaryResult(summary_text=llm_text, state=merged.state, message_count=len(messages))

    def _call_llm(self, messages: list[NormalizedMessage], state: SummaryState) -> str:
        prompt = _build_prompt(messages, state)
        if self.provider == "ollama":
            payload = {
                "model": self.model,
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
                {
                    "role": "system",
                    "content": "Você resume conversas de grupo em português com foco em decisões, pendências, riscos e status atual.",
                },
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
    state_json = json.dumps(asdict(state), ensure_ascii=False)
    timeline = "\n".join(f"- [{m.timestamp.isoformat()}] {m.author}: {m.text}" for m in messages)
    return (
        "Estado anterior (JSON):\n"
        f"{state_json}\n\n"
        "Mensagens novas:\n"
        f"{timeline}\n\n"
        "Responda com Markdown curto contendo:\n"
        "1) Micro-resumo\n2) Decisões\n3) Pendências\n4) Riscos\n5) Status atual"
    )


def _post_json(url: str, payload: dict, timeout_s: int, headers: dict[str, str] | None = None) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url=url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw)

"""
tamaras_client.py
-----------------
Client HTTP para o backend Supabase do JuridicoTamaras.

Autenticação:
    Extrai o JWT de sessão do localStorage do browser via Playwright
    (o perfil persistente deve estar previamente logado em juridicotamaras.com.br).

Uso:
    client = TamarasClient(profile_dir=".tamaras_profile")
    cases  = client.get_gestao_juridico_cases()
    comments = client.get_recent_comments("CASO-2026-016", limit=1)
    client.post_comment("CASO-2026-016", "Status atual: ...")
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Supabase config (público) ─────────────────────────────────────────────────
_SUPABASE_URL = "https://rfimhkckozzyzszxxrfz.supabase.co"
_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InJmaW1oa2Nrb3p6eXpzenh4cmZ6Iiwicm9sZ"
    "SI6ImFub24iLCJpYXQiOjE3NjQxODIwNTcsImV4cCI6MjA3OTc1ODA1N30"
    ".vI7zA106QnpCFG_Km_EG1P7HmEz6mb9lXXok8Ko8wxw"
)
_TAMARAS_URL = "https://juridicotamaras.com.br"
_LS_KEY = "sb-rfimhkckozzyzszxxrfz-auth-token"


class TamarasClient:
    """Acessa o JuridicoTamaras via Supabase REST API autenticado."""

    def __init__(self, profile_dir: str = ".tamaras_profile") -> None:
        self.profile_dir = Path(profile_dir)
        self._token: str | None = None

    # ── Autenticação ──────────────────────────────────────────────────────────

    def _extract_token(self) -> str:
        """Extrai o JWT do localStorage do browser usando Playwright."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright não instalado. Rode: python -m pip install playwright "
                "&& python -m playwright install chromium"
            ) from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                headless=True,
            )
            page = ctx.new_page()
            page.goto(_TAMARAS_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            raw: str | None = page.evaluate(f"localStorage.getItem('{_LS_KEY}')")
            ctx.close()

        if not raw:
            raise RuntimeError(
                f"Sessão do JuridicoTamaras não encontrada no perfil '{self.profile_dir}'. "
                "Abra o browser com headless=False e faça login em juridicotamaras.com.br "
                "antes de executar o pipeline."
            )
        data = json.loads(raw)
        token = data.get("access_token") or ""
        if not token:
            raise RuntimeError("access_token vazio na sessão salva.")
        logger.info("Token do JuridicoTamaras extraído com sucesso.")
        return token

    def _ensure_token(self) -> str:
        if not self._token:
            self._token = self._extract_token()
        return self._token

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": _ANON_KEY,
            "Authorization": f"Bearer {self._ensure_token()}",
            "Content-Type": "application/json",
        }

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _get(self, table: str, params: dict[str, str]) -> Any:
        url = f"{_SUPABASE_URL}/rest/v1/{table}?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=self._headers())
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())

    def _post(self, table: str, body: dict) -> Any:
        url = f"{_SUPABASE_URL}/rest/v1/{table}"
        headers = {**self._headers(), "Prefer": "return=representation"}
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())

    # ── Casos ─────────────────────────────────────────────────────────────────

    def get_gestao_juridico_cases(self) -> list[dict]:
        """Retorna todos os casos em Gestão Jurídico com suas partes."""
        cases: list[dict] = self._get(
            "cases",
            {
                "select": "id,property_address,title,property_important_notes",
                "status": "eq.Em andamento",
                "contract_signed": "eq.false",
                "order": "created_at.desc",
            },
        )
        for case in cases:
            case["parties"] = self._get(
                "parties",
                {
                    "select": "name,party_type",
                    "case_id": f"eq.{case['id']}",
                },
            )
        logger.info("Gestão Jurídico: %d caso(s) encontrado(s).", len(cases))
        return cases

    # ── Comentários ───────────────────────────────────────────────────────────

    def get_recent_comments(self, case_id: str, limit: int = 3) -> list[dict]:
        """Retorna os comentários mais recentes da timeline de um caso."""
        return self._get(
            "case_comments",
            {
                "select": "id,comment,created_at",
                "case_id": f"eq.{case_id}",
                "order": "created_at.desc",
                "limit": str(limit),
            },
        )

    def post_comment(self, case_id: str, comment: str) -> dict:
        """Posta um comentário na timeline do caso. Retorna o registro criado."""
        result = self._post("case_comments", {"case_id": case_id, "comment": comment})
        if isinstance(result, list) and result:
            return result[0]
        return result or {}

    def verify_comment(self, case_id: str, comment_id: str) -> bool:
        """Confirma que o comentário foi persistido no banco."""
        rows = self._get(
            "case_comments",
            {"select": "id", "id": f"eq.{comment_id}", "case_id": f"eq.{case_id}"},
        )
        return isinstance(rows, list) and len(rows) > 0

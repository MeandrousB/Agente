from __future__ import annotations

import asyncio
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path

import streamlit as st

# Garante que o módulo src é encontrado ao rodar com `streamlit run ui.py`
sys.path.insert(0, str(Path(__file__).parent))

# ──────────────────────────────────────────────
# Configuração da página
# ──────────────────────────────────────────────
st.set_page_config(
    page_title="Tamaras — Agente Jurídico",
    page_icon="⚖️",
    layout="wide",
)

# ──────────────────────────────────────────────
# Sidebar — configuração compartilhada
# ──────────────────────────────────────────────
with st.sidebar:
    st.header("🤖 LLM — Ollama")
    ollama_url = st.text_input("URL", value="http://localhost:11434")
    ollama_model = st.text_input("Modelo", value="qwen3:4b")
    llm_timeout = st.number_input("Timeout (s)", value=180, min_value=30, max_value=600, step=30)

    st.divider()

    st.header("📱 WhatsApp Web")
    wa_profile_dir = st.text_input("Perfil WhatsApp", value=".wa_profile")
    wa_headless = st.checkbox("Headless WhatsApp", value=False)
    wa_max_visible = st.number_input(
        "Máx. mensagens visíveis", value=400, min_value=50, max_value=2000, step=50
    )

    st.divider()

    st.header("⚖️ JuridicoTamaras")
    tamaras_profile_dir = st.text_input("Perfil Tamaras", value=".tamaras_profile",
        help="Perfil Chromium para o JuridicoTamaras (deve estar logado).")

    st.divider()

    st.header("💾 Banco de dados")
    db_path = st.text_input("Arquivo SQLite", value="agent.db")

    st.divider()
    st.caption(
        "Na primeira execução o WhatsApp Web abrirá para escanear o QR code. "
        "Após o login, o perfil fica salvo."
    )


# ──────────────────────────────────────────────
# Fix asyncio Windows: força ProactorEventLoop antes do Playwright
# (Streamlit/tornado instala WindowsSelectorEventLoopPolicy, que retorna
# SelectorEventLoop no new_event_loop() — sem suporte a subprocess)
# ──────────────────────────────────────────────
def _fix_event_loop() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


# ══════════════════════════════════════════════════════════════════════════════
# TABS PRINCIPAIS
# ══════════════════════════════════════════════════════════════════════════════
tab_resumo, tab_juridico = st.tabs(["💬 Resumo WhatsApp", "⚖️ Gestão Jurídico"])


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — Resumo WhatsApp (funcionalidade original)
# ══════════════════════════════════════════════════════════════════════════════
with tab_resumo:
    st.header("💬 WhatsApp Group Summarizer")
    st.write(
        "Insira os grupos abaixo e clique em **Resumir**. "
        "O agente abrirá o WhatsApp Web, coletará as mensagens e gerará um resumo com Ollama."
    )

    groups_input = st.text_area(
        "Grupos (um por linha)",
        placeholder="Projeto Alpha\nMarketing Team\nDevOps",
        height=130,
    )

    run_button = st.button("▶ Resumir Grupos", type="primary", use_container_width=True)

    # ── Worker thread ──────────────────────────────────────────────────────
    def _run_wa_pipeline(
        group: str,
        profile_dir: str,
        headless: bool,
        max_visible: int,
        model: str,
        oll_url: str,
        timeout_s: int,
        db: str,
    ) -> str:
        _fix_event_loop()
        from src.agent.collector import PlaywrightWhatsAppCollector
        from src.agent.db import AgentDB
        from src.agent.llm_summarizer import LLMIncrementalSummarizer
        from src.agent.pipeline import WhatsAppSummaryPipeline

        collector = PlaywrightWhatsAppCollector(
            profile_dir=profile_dir, headless=headless, max_messages_visible=max_visible
        )
        summarizer = LLMIncrementalSummarizer(
            provider="ollama", model=model, ollama_url=oll_url, timeout_s=timeout_s
        )
        db_obj = AgentDB(db)
        pipeline = WhatsAppSummaryPipeline(collector=collector, db=db_obj, summarizer=summarizer)
        return pipeline.run_for_group(group)

    # ── Execução ───────────────────────────────────────────────────────────
    if run_button:
        groups = [g.strip() for g in groups_input.strip().splitlines() if g.strip()]
        if not groups:
            st.warning("Insira pelo menos um nome de grupo.")
        else:
            for group in groups:
                st.divider()
                st.subheader(f"📋 {group}")
                thread_timeout = int(llm_timeout) + 300

                with st.spinner(f"Coletando e resumindo '{group}'…"):
                    try:
                        with ThreadPoolExecutor(max_workers=1) as executor:
                            future = executor.submit(
                                _run_wa_pipeline,
                                group,
                                wa_profile_dir,
                                bool(wa_headless),
                                int(wa_max_visible),
                                ollama_model,
                                ollama_url,
                                int(llm_timeout),
                                db_path,
                            )
                            summary = future.result(timeout=thread_timeout)

                        st.success("Resumo gerado com sucesso.")
                        st.markdown(summary)

                        from src.agent.db import AgentDB as _DB
                        db_obj = _DB(db_path)
                        state, checkpoint = db_obj.load_state(group)
                        with st.expander("📊 Estado incremental"):
                            col1, col2, col3 = st.columns(3)
                            with col1:
                                st.write("**Decisões**")
                                for item in state.decisions:
                                    st.write(f"• {item}")
                                if not state.decisions:
                                    st.caption("_(nenhuma)_")
                            with col2:
                                st.write("**Pendências**")
                                for item in state.pending:
                                    st.write(f"• {item}")
                                if not state.pending:
                                    st.caption("_(nenhuma)_")
                            with col3:
                                st.write("**Riscos**")
                                for item in state.risks:
                                    st.write(f"• {item}")
                                if not state.risks:
                                    st.caption("_(nenhum)_")
                            if checkpoint:
                                st.caption(f"Checkpoint: {checkpoint.strftime('%d/%m/%Y %H:%M')}")

                        latest = db_obj.get_latest_summary(group)
                        if latest:
                            with st.expander("📚 Último resumo salvo"):
                                st.caption(
                                    f"Gerado em {latest['created_at']} — {latest['message_count']} mensagens"
                                )
                                st.markdown(str(latest["summary_text"]))

                    except FuturesTimeoutError:
                        st.error(
                            f"Tempo esgotado ao processar '{group}'. "
                            "Tente aumentar o Timeout (s) na sidebar."
                        )
                    except RuntimeError as exc:
                        _msg = str(exc)
                        if "Playwright" in _msg and "instalado" in _msg:
                            st.error(
                                "Playwright não instalado. Rode:\n\n"
                                "```\npython -m pip install playwright\n"
                                "python -m playwright install chromium\n```"
                            )
                        else:
                            st.error(f"Erro ao processar '{group}': {_msg}")
                            with st.expander("🔍 Traceback"):
                                st.code(traceback.format_exc(), language="python")
                    except Exception as exc:
                        st.error(f"Erro inesperado em '{group}': {type(exc).__name__}: {exc}")
                        with st.expander("🔍 Traceback"):
                            st.code(traceback.format_exc(), language="python")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — Gestão Jurídico
# ══════════════════════════════════════════════════════════════════════════════
with tab_juridico:
    st.header("⚖️ Gestão Jurídico — Atualização de Timeline")
    st.write(
        "O agente busca todos os casos em **Gestão Jurídico** no JuridicoTamaras, "
        "localiza os grupos de WhatsApp de cada caso, coleta o contexto e "
        "posta um comentário estruturado na timeline."
    )

    col_info, col_btn = st.columns([3, 1])
    with col_info:
        st.info(
            "**Antes de começar:** certifique-se de que o perfil Tamaras "
            f"(`{tamaras_profile_dir}`) está logado em juridicotamaras.com.br "
            "e que o WhatsApp Web está ativo no perfil selecionado."
        )
    with col_btn:
        run_juridico = st.button(
            "▶ Processar Casos",
            type="primary",
            use_container_width=True,
            key="run_juridico",
        )

    # ── Worker thread ──────────────────────────────────────────────────────
    def _run_legal_pipeline(
        tamaras_profile: str,
        wa_profile: str,
        wa_hdl: bool,
        wa_max: int,
        model: str,
        oll_url: str,
        timeout_s: int,
        progress_queue,  # multiprocessing.Queue or list for progress updates
    ) -> list:
        """Runs the full legal pipeline in a background thread."""
        _fix_event_loop()
        from src.agent.legal_pipeline import LegalCasePipeline
        from src.agent.tamaras_client import TamarasClient

        tamaras = TamarasClient(profile_dir=tamaras_profile)
        pipeline = LegalCasePipeline(
            tamaras_client=tamaras,
            wa_profile_dir=wa_profile,
            wa_headless=wa_hdl,
            wa_max_messages=wa_max,
            ollama_model=model,
            ollama_url=oll_url,
            llm_timeout_s=timeout_s,
        )

        def _cb(idx: int, total: int, case_id: str, step: str) -> None:
            try:
                progress_queue.append((idx, total, case_id, step))
            except Exception:
                pass

        results = pipeline.run(progress_cb=_cb)
        # Return serializable dict per result
        return [
            {
                "case_id": r.case_id,
                "property_address": r.property_address,
                "groups_found": r.groups_found,
                "groups_searched": r.groups_searched,
                "message_count": r.message_count,
                "generated_comment": r.generated_comment,
                "posted_comment_id": r.posted_comment_id,
                "verified": r.verified,
                "error": r.error,
                "skipped": r.skipped,
                "skip_reason": r.skip_reason,
                "success": r.success,
                "summary_line": r.summary_line(),
            }
            for r in results
        ]

    # ── Execução ───────────────────────────────────────────────────────────
    if run_juridico:
        progress_queue: list = []
        progress_placeholder = st.empty()
        results_placeholder = st.container()

        # Timeout: 9 casos × (WhatsApp ~2 min + LLM ~3 min) + margem = ~60 min
        legal_timeout = 9 * (120 + int(llm_timeout) + 60) + 300

        with st.spinner("Processando casos de Gestão Jurídico…"):
            try:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(
                        _run_legal_pipeline,
                        tamaras_profile_dir,
                        wa_profile_dir,
                        bool(wa_headless),
                        int(wa_max_visible),
                        ollama_model,
                        ollama_url,
                        int(llm_timeout),
                        progress_queue,
                    )

                    # Poll for progress while waiting
                    import time
                    while not future.done():
                        if progress_queue:
                            idx, total, case_id, step = progress_queue[-1]
                            pct = (idx + 0.5) / max(total, 1)
                            progress_placeholder.progress(
                                pct,
                                text=f"[{idx+1}/{total}] {case_id} — {step}",
                            )
                        time.sleep(1)

                    results = future.result(timeout=legal_timeout)

                progress_placeholder.progress(1.0, text="Concluído!")

            except FuturesTimeoutError:
                st.error(
                    "Tempo esgotado. O processamento de 9 casos pode demorar bastante. "
                    "Tente aumentar o timeout do LLM ou processar menos casos de uma vez."
                )
                results = []
            except Exception as exc:
                st.error(f"Erro no pipeline jurídico: {type(exc).__name__}: {exc}")
                with st.expander("🔍 Traceback"):
                    st.code(traceback.format_exc(), language="python")
                results = []

        # ── Exibir resultados por caso ─────────────────────────────────────
        if results:
            st.divider()
            st.subheader(f"Resultados — {len(results)} caso(s) processado(s)")

            ok = sum(1 for r in results if r["success"])
            skip = sum(1 for r in results if r["skipped"])
            err = sum(1 for r in results if r["error"] and not r["skipped"])

            m1, m2, m3 = st.columns(3)
            m1.metric("✅ Comentados", ok)
            m2.metric("⏭ Pulados", skip)
            m3.metric("❌ Erros", err)

            st.divider()

            for r in results:
                icon = "✅" if r["success"] else ("⏭" if r["skipped"] else "❌")
                label = f"{icon} {r['case_id']} — {r['property_address']}"

                with st.expander(label, expanded=r.get("error", "") != "" and not r["skipped"]):
                    st.caption(r["summary_line"])

                    if r["groups_found"]:
                        st.write(f"**Grupos encontrados:** {', '.join(r['groups_found'])}")
                    elif r["groups_searched"]:
                        st.write(f"**Termos buscados:** {', '.join(r['groups_searched'])}")

                    if r["message_count"]:
                        st.write(f"**Mensagens coletadas:** {r['message_count']}")

                    if r["generated_comment"]:
                        with st.expander("📝 Comentário gerado"):
                            st.text(r["generated_comment"])

                    if r["posted_comment_id"]:
                        case_url = (
                            f"https://juridicotamaras.com.br/casos/{r['case_id']}"
                        )
                        st.markdown(
                            f"[🔗 Ver na timeline]({case_url})"
                            f" — ID: `{r['posted_comment_id'][:8]}…`"
                            f" — {'✅ Verificado' if r['verified'] else '⚠ Não verificado'}"
                        )

                    if r["error"]:
                        st.error(r["error"])

                    if r["skip_reason"]:
                        st.warning(r["skip_reason"])

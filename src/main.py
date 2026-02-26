from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.agent.collector import JsonFileCollector, MessageCollector, MockCollector, PlaywrightWhatsAppCollector
from src.agent.db import AgentDB
from src.agent.llm_summarizer import LLMIncrementalSummarizer
from src.agent.pipeline import WhatsAppSummaryPipeline
from src.agent.summarizer import IncrementalSummarizer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resumo incremental de grupos WhatsApp")
    parser.add_argument("--group", default="Projeto X", help="Nome do grupo")
    parser.add_argument("--db", default="agent.db", help="Caminho do SQLite")

    parser.add_argument(
        "--source",
        choices=["mock", "json", "whatsapp-web"],
        default="mock",
        help="Fonte de coleta: mock, json, whatsapp-web.",
    )
    parser.add_argument("--source-json", help="Arquivo JSON com mensagens por grupo (usado com --source json).")

    parser.add_argument("--wa-profile-dir", default=".wa_profile", help="Perfil persistente do Chromium no modo whatsapp-web.")
    parser.add_argument("--wa-headless", action="store_true", help="Executa navegador em headless no modo whatsapp-web.")
    parser.add_argument(
        "--wa-max-visible",
        type=int,
        default=300,
        help="Máximo de mensagens visíveis extraídas por execução no modo whatsapp-web.",
    )

    parser.add_argument("--llm-provider", choices=["none", "ollama", "openai"], default="none")
    parser.add_argument("--llm-model", default="")
    parser.add_argument("--ollama-url", default="http://localhost:11434")
    parser.add_argument("--openai-base-url", default="https://api.openai.com")
    parser.add_argument("--openai-api-key-env", default="OPENAI_API_KEY")

    parser.add_argument("--output", help="Arquivo para salvar o micro-resumo gerado (ex.: out/resumo.md).")
    parser.add_argument(
        "--show-state",
        action="store_true",
        help="Exibe o estado incremental (decisões/pendências/riscos/status) após a execução.",
    )
    return parser


def build_collector(args: argparse.Namespace) -> MessageCollector:
    if args.source == "mock":
        return MockCollector()
    if args.source == "json":
        if not args.source_json:
            raise ValueError("Com --source json, informe --source-json <arquivo>. ")
        return JsonFileCollector(args.source_json)
    return PlaywrightWhatsAppCollector(
        profile_dir=args.wa_profile_dir,
        headless=args.wa_headless,
        max_messages_visible=args.wa_max_visible,
    )


def main() -> None:
    args = build_parser().parse_args()
    collector = build_collector(args)

    db = AgentDB(args.db)
    pipeline = WhatsAppSummaryPipeline(
        collector=collector,
        db=db,
        summarizer=IncrementalSummarizer(),
    )
    summary = pipeline.run_for_group(args.group)
    print(summary)

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(summary, encoding="utf-8")
        print(f"\nResumo salvo em: {output_path}")

    if args.show_state:
        state, checkpoint = db.load_state(args.group)
        payload = {
            "group": args.group,
            "checkpoint_last_message_ts": checkpoint.isoformat() if checkpoint else None,
            "decisions": state.decisions,
            "pending": state.pending,
            "risks": state.risks,
            "current_status": state.current_status,
        }
        print("\nEstado incremental:")
        print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

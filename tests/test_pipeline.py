from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.agent.collector import JsonFileCollector, MessageCollector, MockCollector
from src.agent.collector import JsonFileCollector, MockCollector
from src.agent.db import AgentDB
from src.agent.pipeline import WhatsAppSummaryPipeline
from src.agent.summarizer import IncrementalSummarizer


class EmptyCollector(MessageCollector):
    def collect_messages(self, group_name: str, since_timestamp=None):
        return []


class PipelineTestCase(unittest.TestCase):
    def test_incremental_summary_and_noise_filter_with_mock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = AgentDB(f"{tmp}/agent.db")
            pipeline = WhatsAppSummaryPipeline(MockCollector(), db, IncrementalSummarizer())

            summary_1 = pipeline.run_for_group("Grupo Teste")
            self.assertIn("Micro-resumo", summary_1)
            self.assertIn("Decisões: 1", summary_1)
            self.assertIn("Pendências: 1", summary_1)
            self.assertIn("Riscos: 1", summary_1)

            summary_2 = pipeline.run_for_group("Grupo Teste")
            self.assertIn("Sem novas mensagens", summary_2)

    def test_json_collector_and_checkpoint_by_last_message_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = AgentDB(f"{tmp}/agent.db")
            payload_path = Path(tmp) / "messages.json"
            payload = {
                "Grupo A": [
                    {
                        "author": "Ana",
                        "timestamp": "2026-01-01T10:00:00",
                        "text": "Decisão: publicar ata.",
                        "external_id": "a1",
                    },
                    {
                        "author": "Bruno",
                        "timestamp": "2026-01-01T10:05:00",
                        "text": "Pendente: validar escopo.",
                        "external_id": "a2",
                    },
                ]
            }
            payload_path.write_text(json.dumps(payload), encoding="utf-8")

            pipeline = WhatsAppSummaryPipeline(
                JsonFileCollector(str(payload_path)),
                db,
                IncrementalSummarizer(),
            )

            summary_1 = pipeline.run_for_group("Grupo A")
            self.assertIn("Decisões: 1", summary_1)
            self.assertIn("Pendências: 1", summary_1)

            payload["Grupo A"].append(
                {
                    "author": "Carla",
                    "timestamp": "2026-01-01T10:06:00",
                    "text": "Risco: atraso no fornecedor.",
                    "external_id": "a3",
                }
            )
            payload_path.write_text(json.dumps(payload), encoding="utf-8")

            summary_2 = pipeline.run_for_group("Grupo A")
            self.assertIn("Riscos: 1", summary_2)
            self.assertIn("Carla", summary_2)


    def test_first_run_with_no_collected_messages_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = AgentDB(f"{tmp}/agent.db")
            pipeline = WhatsAppSummaryPipeline(EmptyCollector(), db, IncrementalSummarizer())
            with self.assertRaises(RuntimeError):
                pipeline.run_for_group("Grupo Vazio")


    def test_json_collector_group_not_found_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = AgentDB(f"{tmp}/agent.db")
            payload_path = Path(tmp) / "messages.json"
            payload_path.write_text(json.dumps({"Outro Grupo": []}), encoding="utf-8")

            pipeline = WhatsAppSummaryPipeline(
                JsonFileCollector(str(payload_path)),
                db,
                IncrementalSummarizer(),
            )

            with self.assertRaises(ValueError):
                pipeline.run_for_group("Grupo inexistente")

if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

from src.agent.collector import JsonFileCollector, MockCollector, PlaywrightWhatsAppCollector
from src.agent.llm_summarizer import LLMIncrementalSummarizer
from src.agent.summarizer import IncrementalSummarizer
from src.main import build_collector, build_parser, build_summarizer


class MainTestCase(unittest.TestCase):
    def test_build_collector_mock_default(self) -> None:
        args = build_parser().parse_args([])
        collector = build_collector(args)
        self.assertIsInstance(collector, MockCollector)

    def test_build_collector_json_requires_file(self) -> None:
        args = build_parser().parse_args(["--source", "json"])
        with self.assertRaises(ValueError):
            build_collector(args)

    def test_build_collector_json(self) -> None:
        args = build_parser().parse_args(["--source", "json", "--source-json", "data/sample_messages.json"])
        collector = build_collector(args)
        self.assertIsInstance(collector, JsonFileCollector)

    def test_build_collector_whatsapp_web(self) -> None:
        args = build_parser().parse_args(["--source", "whatsapp-web", "--wa-profile-dir", "/tmp/wa"])
        collector = build_collector(args)
        self.assertIsInstance(collector, PlaywrightWhatsAppCollector)

    def test_build_summarizer_default(self) -> None:
        args = build_parser().parse_args([])
        summarizer = build_summarizer(args)
        self.assertIsInstance(summarizer, IncrementalSummarizer)

    def test_build_summarizer_llm_requires_model(self) -> None:
        args = build_parser().parse_args(["--llm-provider", "ollama"])
        with self.assertRaises(ValueError):
            build_summarizer(args)

    def test_build_summarizer_llm(self) -> None:
        args = build_parser().parse_args(["--llm-provider", "ollama", "--llm-model", "qwen2.5:7b"])
        summarizer = build_summarizer(args)
        self.assertIsInstance(summarizer, LLMIncrementalSummarizer)

    def test_doctor_flag_parses(self) -> None:
        args = build_parser().parse_args(["--doctor"])
        self.assertTrue(args.doctor)


if __name__ == "__main__":
    unittest.main()

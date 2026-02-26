from __future__ import annotations

import unittest

from src.main import build_collector, build_parser
from src.agent.collector import JsonFileCollector, MockCollector, PlaywrightWhatsAppCollector


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


if __name__ == "__main__":
    unittest.main()

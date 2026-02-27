from __future__ import annotations

import unittest

from src.agent.collector import _extract_author_and_timestamp, extract_author_and_timestamp
from src.agent.collector import _extract_author_and_timestamp


class CollectorHelpersTestCase(unittest.TestCase):
    def test_extract_author_and_timestamp_with_date(self) -> None:
        author, ts = extract_author_and_timestamp("[10:18, 22/02/2026] Pedro: ")
        author, ts = _extract_author_and_timestamp("[10:18, 22/02/2026] Pedro: ")
        self.assertEqual(author, "Pedro")
        self.assertEqual(ts.year, 2026)
        self.assertEqual(ts.month, 2)
        self.assertEqual(ts.day, 22)
        self.assertEqual(ts.hour, 10)
        self.assertEqual(ts.minute, 18)

    def test_extract_author_and_timestamp_invalid_meta(self) -> None:
        author, ts = _extract_author_and_timestamp("texto inválido")
        self.assertEqual(author, "desconhecido")
        self.assertIsNotNone(ts)


if __name__ == "__main__":
    unittest.main()

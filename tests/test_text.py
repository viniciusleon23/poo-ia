from __future__ import annotations

import unittest

from app.text import split_for_discord


class SplitForDiscordTests(unittest.TestCase):
    def test_short_text_remains_one_message(self) -> None:
        self.assertEqual(split_for_discord("Hola"), ["Hola"])

    def test_prefers_word_boundaries(self) -> None:
        chunks = split_for_discord("uno dos tres cuatro", limit=10)

        self.assertEqual(chunks, ["uno dos", "tres", "cuatro"])
        self.assertTrue(all(len(chunk) <= 10 for chunk in chunks))

    def test_splits_a_single_long_word(self) -> None:
        text = "x" * 23
        chunks = split_for_discord(text, limit=10)

        self.assertEqual(chunks, ["x" * 10, "x" * 10, "x" * 3])
        self.assertEqual("".join(chunks), text)

    def test_empty_text_produces_no_messages(self) -> None:
        self.assertEqual(split_for_discord(" \n "), [])

    def test_rejects_invalid_limits(self) -> None:
        with self.assertRaises(ValueError):
            split_for_discord("texto", limit=0)

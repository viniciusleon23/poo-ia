from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.prompt_loader import build_prompt, load_prompt_context


class PromptLoaderTests(unittest.TestCase):
    def test_loads_supported_files_in_a_deterministic_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "rules").mkdir()
            (root / "personality").mkdir()
            (root / "rules" / "zeta.md").write_text("Zeta", encoding="utf-8")
            (root / "rules" / "alpha.txt").write_text("Alpha", encoding="utf-8")
            (root / "rules" / "ignored.py").write_text("ignored", encoding="utf-8")
            (root / "personality" / "voice.md").write_text("Calma", encoding="utf-8")

            context = load_prompt_context(root)

        self.assertLess(context.index("Alpha"), context.index("Zeta"))
        self.assertLess(context.index("Zeta"), context.index("Calma"))
        self.assertIn("## Reglas: alpha", context)
        self.assertIn("## Personalidad: voice", context)
        self.assertNotIn("ignored", context)

    def test_missing_content_directories_are_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            self.assertEqual(load_prompt_context(Path(temporary_directory)), "")

    def test_build_prompt_keeps_instructions_and_user_message_separate(self) -> None:
        prompt = build_prompt("Regla útil", "Hola, Poo-IA")

        self.assertIn("Regla útil", prompt)
        self.assertIn("## Mensaje del usuario\n\nHola, Poo-IA", prompt)
        self.assertTrue(prompt.endswith("## Respuesta"))

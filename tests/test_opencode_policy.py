from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class OpenCodePolicyTests(unittest.TestCase):
    def test_global_policy_denies_unknown_tools_and_content_search(self) -> None:
        config = json.loads((ROOT / "opencode/opencode.jsonc").read_text())
        permission = config["permission"]

        self.assertEqual(config["model"], "openai/gpt-5.4-mini")
        self.assertEqual(next(iter(permission.items())), ("*", "deny"))
        self.assertEqual(permission["read"], "allow")
        self.assertEqual(permission["grep"], "deny")
        self.assertEqual(permission["bash"], "deny")
        self.assertEqual(permission["task"], "deny")
        self.assertEqual(permission["external_directory"], "deny")

    def test_every_agent_repeats_fail_closed_boundary(self) -> None:
        for path in sorted((ROOT / "opencode/agents").glob("*.md")):
            text = path.read_text(encoding="utf-8")
            with self.subTest(agent=path.name):
                self.assertIn('  "*": deny', text)
                self.assertIn("  grep: deny", text)
                self.assertIn("  bash: deny", text)
                self.assertIn("  external_directory: deny", text)
                self.assertNotIn("  grep: allow", text)

    def test_service_uses_sanitized_view_and_isolated_home(self) -> None:
        service = (ROOT / "ops/opencode-capnet.service").read_text(encoding="utf-8")

        self.assertIn("WorkingDirectory=-/home/poo/capnet-research-view", service)
        self.assertIn("Environment=HOME=/home/poo/.local/share/poo-ia-opencode-home", service)
        self.assertIn("worker/research_view.py --source /home/poo/capnet-workspace", service)
        self.assertNotIn("WorkingDirectory=/home/poo/capnet-workspace", service)

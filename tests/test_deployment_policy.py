from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).parent.parent


class DeploymentPolicyTests(unittest.TestCase):
    def test_normal_container_is_nonroot_and_drops_all_capabilities(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        compose = (ROOT / "compose.yml").read_text(encoding="utf-8")

        self.assertIn("USER 10001:10001", dockerfile)
        self.assertIn("cap_drop:\n      - ALL", compose)
        self.assertIn("no-new-privileges:true", compose)

    def test_documented_maintenance_grants_only_the_required_capability(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        operations = (ROOT / "docs" / "operations.md").read_text(encoding="utf-8")

        chown_helper = "--cap-add CHOWN --cap-add DAC_OVERRIDE"
        self.assertEqual(readme.count(chown_helper), 1)
        self.assertEqual(operations.count(chown_helper), 2)
        self.assertEqual(
            operations.count("--cap-add DAC_OVERRIDE"), 5
        )
        self.assertNotIn("--privileged", readme + operations)

    def test_both_host_services_disable_core_dumps(self) -> None:
        for unit_name in ("opencode-capnet.service", "poo-ia-worker.service"):
            with self.subTest(unit=unit_name):
                unit = (ROOT / "ops" / unit_name).read_text(encoding="utf-8")
                self.assertIn("LimitCORE=0", unit)

    def test_private_env_names_are_ignored_but_examples_are_versionable(self) -> None:
        for ignore_name in (".gitignore", ".dockerignore"):
            with self.subTest(ignore=ignore_name):
                patterns = (ROOT / ignore_name).read_text(encoding="utf-8")
                self.assertIn("*.env\n", patterns)
                self.assertIn("*.env.*\n", patterns)
                self.assertIn("!*.env.example\n", patterns)


if __name__ == "__main__":
    unittest.main()

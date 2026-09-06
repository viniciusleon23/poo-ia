from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from worker.job_process import _load_detached_settings


class DetachedJobSettingsTests(unittest.TestCase):
    def test_settings_load_without_restoring_worker_api_password(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = _load_detached_settings()

            self.assertNotIn("WORKER_PASSWORD", os.environ)
            self.assertEqual(settings.password, "unused-by-detached-worker")


if __name__ == "__main__":
    unittest.main()

"""Unit tests for Waguri (save-restricted-bot) upgrades."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import storage


class WaguriUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.old_db = storage.DB_PATH
        storage.DB_PATH = os.path.join(self.tmpdir.name, "test.sqlite3")
        storage.init_storage()

    def tearDown(self):
        storage.DB_PATH = self.old_db
        self.tmpdir.cleanup()

    def test_transfer_stats_and_checkpoints(self):
        storage.record_transfer_stats(123, files=3, bytes_transferred=1024 * 1024, failed=1, batches=1)
        storage.record_transfer_stats(123, files=2, bytes_transferred=512 * 1024, failed=0, batches=0)
        stats = storage.get_transfer_stats(123)
        self.assertEqual(stats["total_files"], 5)
        self.assertEqual(stats["total_bytes"], 1536 * 1024)
        self.assertEqual(stats["failed_items"], 1)
        self.assertEqual(stats["total_batches"], 1)

        storage.save_batch_checkpoint(
            123,
            status="paused",
            specs=[{"start_link": "https://t.me/c/1/1", "end_link": "https://t.me/c/1/50", "metadata": {}}],
            last_range_index=0,
            last_message_id=25,
            success_count=20,
            total_count=50,
        )
        cp = storage.get_batch_checkpoint(123)
        self.assertIsNotNone(cp)
        self.assertEqual(cp["status"], "paused")
        self.assertEqual(cp["last_message_id"], 25)
        self.assertEqual(len(cp["specs"]), 1)
        self.assertTrue(storage.clear_batch_checkpoint(123))
        self.assertIsNone(storage.get_batch_checkpoint(123))

    def test_media_filter_preference_validation(self):
        storage.update_preference(123, "media_filter", " Video , Document, video ")
        prefs = storage.get_preferences(123)
        self.assertEqual(prefs["media_filter"], "video,document")

    def test_transfer_split_caption_newline_and_progress_speed(self):
        src = (ROOT / "plugins/transfer.py").read_text(encoding="utf-8")
        self.assertNotIn('f"{source_caption or \'\'}\\\\n\\\\n{pref_caption}"', src)
        self.assertIn("speed_str", src)
        self.assertIn("eta_str", src)
        self.assertIn("except FloodWait as exc:", src)

    def test_save_and_main_wiring_for_pause_resume_filter_and_resetguard(self):
        save_src = (ROOT / "plugins/save.py").read_text(encoding="utf-8")
        main_src = (ROOT / "main.py").read_text(encoding="utf-8")
        stats_src = (ROOT / "plugins/stats.py").read_text(encoding="utf-8")
        settings_src = (ROOT / "plugins/settings.py").read_text(encoding="utf-8")
        helpers_src = (ROOT / "plugins/helpers.py").read_text(encoding="utf-8")

        self.assertIn("def reset(self)", helpers_src)
        self.assertIn('filters.command("pause")', save_src)
        self.assertIn('filters.command("resume")', save_src)
        self.assertIn('"--only"', save_src)
        self.assertIn('"--skip"', save_src)
        self.assertIn('filters.command("setfilter")', settings_src)
        self.assertIn('filters.command("resetguard")', stats_src)
        for cmd in ('"pause"', '"resume"', '"setfilter"', '"resetguard"'):
            self.assertIn(cmd, main_src)


if __name__ == "__main__":
    unittest.main()

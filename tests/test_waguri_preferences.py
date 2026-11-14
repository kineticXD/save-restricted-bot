"""Dependency-free smoke tests for Waguri's persistent transfer preferences."""
import os
import tempfile
import unittest

import storage


class PreferenceStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="waguri-test-")
        self.old_path = storage.DB_PATH
        storage.DB_PATH = os.path.join(self.tmp.name, "test.sqlite3")
        storage.init_storage()

    def tearDown(self):
        storage.DB_PATH = self.old_path
        self.tmp.cleanup()

    def test_defaults_and_persistence(self):
        prefs = storage.get_preferences(424242)
        self.assertEqual(prefs["caption_mode"], "keep")
        self.assertEqual(prefs["caption_text"], "")
        self.assertEqual(prefs["auto_pin"], 0)
        self.assertEqual(prefs["replace_rules"], [])

        storage.update_preference(424242, "caption_mode", "append")
        storage.update_preference(424242, "caption_text", "Week 1")
        storage.update_preference(424242, "thumb_path", "/data/custom_thumbs/424242.jpg")
        storage.update_preference(424242, "auto_pin", True)
        storage.update_preference(424242, "replace_rules", [{"old": "old", "new": "new"}])

        prefs = storage.get_preferences(424242)
        self.assertEqual(prefs["caption_mode"], "append")
        self.assertEqual(prefs["caption_text"], "Week 1")
        self.assertEqual(prefs["thumb_path"], "/data/custom_thumbs/424242.jpg")
        self.assertEqual(prefs["thumb_mode"], "source")
        self.assertEqual(prefs["auto_pin"], 1)
        self.assertEqual(prefs["replace_rules"], [{"old": "old", "new": "new"}])

    def test_thumbnail_modes_are_validated_and_persisted(self):
        storage.update_preference(424242, "thumb_mode", "none")
        self.assertEqual(storage.get_preferences(424242)["thumb_mode"], "none")
        with self.assertRaises(ValueError):
            storage.update_preference(424242, "thumb_mode", "random")

    def test_old_database_migrates_thumbnail_mode(self):
        old_db = os.path.join(self.tmp.name, "old.sqlite3")
        import sqlite3
        connection = sqlite3.connect(old_db)
        connection.execute("""CREATE TABLE transfer_preferences (
            owner_id INTEGER PRIMARY KEY,
            caption_mode TEXT NOT NULL DEFAULT 'keep',
            caption_text TEXT NOT NULL DEFAULT '',
            thumb_path TEXT NOT NULL DEFAULT '',
            auto_pin INTEGER NOT NULL DEFAULT 0,
            replace_rules TEXT NOT NULL DEFAULT '[]',
            updated_at TEXT NOT NULL
        )""")
        connection.execute(
            "INSERT INTO transfer_preferences (owner_id, updated_at) VALUES (?, ?)",
            (5151, "2026-01-01T00:00:00+00:00"),
        )
        connection.commit()
        connection.close()

        current_path = storage.DB_PATH
        storage.DB_PATH = old_db
        try:
            storage.init_storage()
            self.assertEqual(storage.get_preferences(5151)["thumb_mode"], "source")
            storage.update_preference(5151, "thumb_mode", "none")
            self.assertEqual(storage.get_preferences(5151)["thumb_mode"], "none")
        finally:
            storage.DB_PATH = current_path

    def test_invalid_caption_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            storage.update_preference(424242, "caption_mode", "random")

    def test_unknown_preference_is_rejected(self):
        with self.assertRaises(ValueError):
            storage.update_preference(424242, "not_a_setting", "x")


if __name__ == "__main__":
    unittest.main()

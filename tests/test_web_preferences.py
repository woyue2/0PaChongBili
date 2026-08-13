import os
import sys
import tempfile
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.common import web_preferences


class WebPreferencesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = web_preferences.DB_FILE
        web_preferences.DB_FILE = os.path.join(self.tmp.name, "preferences.db")

    def tearDown(self):
        web_preferences.DB_FILE = self.original
        self.tmp.cleanup()

    def test_hidden_day_persists_and_can_be_restored(self):
        web_preferences.set_hidden("bili", "av1", "2026-08-14", True)
        self.assertEqual(
            web_preferences.list_hidden_days("bili", ["av1"]),
            {"av1": ["2026-08-14"]},
        )
        web_preferences.set_hidden("bili", "av1", "2026-08-14", False)
        self.assertEqual(web_preferences.list_hidden_days("bili", ["av1"]), {"av1": []})

    def test_visibility_is_scoped_by_platform_and_video(self):
        web_preferences.set_hidden("xhs", "same", "2026-08-14", True)
        self.assertEqual(web_preferences.list_hidden_days("bili", ["same"]), {"same": []})
        self.assertEqual(web_preferences.list_hidden_days("xhs", ["other"]), {"other": []})

    def test_record_note_persists_and_updates(self):
        saved = web_preferences.save_record_note("xhs", "same", "第一版")
        self.assertEqual(saved["content"], "第一版")
        web_preferences.save_record_note("xhs", "same", "第二版")
        self.assertEqual(
            web_preferences.list_record_notes("xhs", ["same"])["same"]["content"],
            "第二版",
        )

    def test_record_notes_are_scoped_and_empty_content_clears(self):
        web_preferences.save_record_note("xhs", "same", "小红书笔记")
        web_preferences.save_record_note("bili", "same", "B站笔记")
        notes = web_preferences.list_record_notes("xhs", ["same"])
        self.assertEqual(notes["same"]["content"], "小红书笔记")
        self.assertEqual(web_preferences.list_record_notes("bili", ["same"])["same"]["content"], "B站笔记")
        web_preferences.save_record_note("xhs", "same", "")
        self.assertEqual(web_preferences.list_record_notes("xhs", ["same"]), {})

    def test_record_note_length_is_limited(self):
        with self.assertRaises(ValueError):
            web_preferences.save_record_note("xhs", "same", "x" * 10001)
        with self.assertRaises(ValueError):
            web_preferences.save_record_note("xhs", "same", {"text": "无效"})


if __name__ == "__main__":
    unittest.main()

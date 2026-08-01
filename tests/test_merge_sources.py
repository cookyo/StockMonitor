import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

import merge_sources as ms


class MergeDiscoveryTests(unittest.TestCase):
    def test_discover_groups_each_slot_independently(self) -> None:
        day = dt.date(2026, 8, 1)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in (
                "baidu_688981_2026-08-01_早盘.json",
                "sina_sh688981_2026-08-01_早盘.json",
                "baidu_688981_2026-08-01_尾盘.json",
                "sina_sh688981_2026-08-01.json",
            ):
                (root / name).touch()

            all_slots = ms._discover(day, data_dir=root)
            morning = ms._discover(day, slot="早盘", data_dir=root)

        self.assertEqual({("688981", ""), ("688981", "早盘"), ("688981", "尾盘")}, set(all_slots))
        self.assertEqual({"baidu", "sina"}, set(morning[("688981", "早盘")]))
        self.assertNotIn(("688981", "尾盘"), morning)

    def test_merge_deduplicates_and_sets_sina_provider(self) -> None:
        shared = "这是一段跨来源完全相同且足够长的正文，用来验证去重逻辑"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baidu = root / "baidu.json"
            sina = root / "sina.json"
            baidu.write_text(json.dumps([
                {"post_id": "b1", "body": shared, "published_at": "2026-08-01 10:00:00"},
            ]), encoding="utf-8")
            sina.write_text(json.dumps([
                {"post_id": "s1", "body": shared, "published_at": "2026-08-01 09:00:00"},
                {"post_id": "s2", "body": "新浪独有正文", "published_at": "2026-08-01 11:00:00"},
            ]), encoding="utf-8")

            rows = ms.merge_one("688981", {"baidu": str(baidu), "sina": str(sina)})

        self.assertEqual(2, len(rows))
        sina_row = next(row for row in rows if row["post_id"] == "s2")
        self.assertEqual("新浪", sina_row["provider"])
        self.assertTrue(all(row["stock_code"] == "688981" for row in rows))


if __name__ == "__main__":
    unittest.main()

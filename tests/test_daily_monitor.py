import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import daily_monitor as dm
import fetch_em_api


def result(source: str, *, error: str = "", posts=None, path: str = "") -> dict:
    return {
        "source": source,
        "symbol": "688981",
        "pages": 1,
        "posts": [] if posts is None else posts,
        "path": path,
        "error": error,
    }


class SourcePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.item = {"code": "688981", "type": "stock", "name": "中芯国际"}
        self.day = dt.date(2026, 8, 1)

    @mock.patch.object(dm, "fetch_item")
    def test_primary_success_does_not_request_backups(self, fetch_item: mock.Mock) -> None:
        fetch_item.return_value = result("eastmoney", posts=[{"post_id": "1"}], path="eastmoney.json")

        rows = dm.fetch_item_with_policy(self.item, self.day, source="auto", slot="早盘")

        self.assertEqual(["eastmoney"], [row["source"] for row in rows])
        fetch_item.assert_called_once_with(
            self.item, self.day, source="eastmoney", slot="早盘",
        )

    @mock.patch.object(dm, "fetch_item")
    def test_primary_empty_success_does_not_request_backups(self, fetch_item: mock.Mock) -> None:
        primary = result("eastmoney")
        primary["empty_skipped"] = True
        fetch_item.return_value = primary

        rows = dm.fetch_item_with_policy(self.item, self.day, source="auto", slot="早盘")

        self.assertEqual(1, len(rows))
        fetch_item.assert_called_once()

    @mock.patch.object(dm.ms, "save_merged")
    @mock.patch.object(dm.ms, "merge_one")
    @mock.patch.object(dm, "fetch_item")
    def test_primary_failure_requests_both_backups_and_merges(
        self, fetch_item: mock.Mock, merge_one: mock.Mock, save_merged: mock.Mock,
    ) -> None:
        by_source = {
            "eastmoney": result("eastmoney", error="限流"),
            "baidu": result("baidu", posts=[{"post_id": "b"}], path="baidu.json"),
            "sina": result("sina", posts=[{"post_id": "s"}], path="sina.json"),
        }
        fetch_item.side_effect = lambda _item, _day, source, slot: by_source[source]
        merge_one.return_value = [{"post_id": "b"}, {"post_id": "s"}]
        save_merged.return_value = (Path("merged.csv"), Path("merged.json"))

        rows = dm.fetch_item_with_policy(self.item, self.day, source="auto", slot="尾盘")

        self.assertEqual(["eastmoney", "baidu", "sina"], [row["source"] for row in rows])
        self.assertEqual(
            ["eastmoney", "baidu", "sina"],
            [call.kwargs["source"] for call in fetch_item.call_args_list],
        )
        merge_one.assert_called_once_with(
            "688981", {"baidu": "baidu.json", "sina": "sina.json"},
        )
        self.assertEqual("merged.json", rows[1]["merged_path"])
        self.assertEqual("merged.json", rows[2]["merged_path"])


class ConfigAndExitCodeTests(unittest.TestCase):
    def test_eastmoney_ssl_context_keeps_verification_enabled(self) -> None:
        self.assertEqual(fetch_em_api.ssl.CERT_REQUIRED, fetch_em_api._SSL_CTX.verify_mode)
        self.assertTrue(fetch_em_api._SSL_CTX.check_hostname)

    def test_load_config_applies_defaults_and_rejects_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({
                "defaults": {"max_posts": 100},
                "items": [{"code": "688981"}],
            }), encoding="utf-8")
            config = dm.load_config(path)
            self.assertEqual(100, config["items"][0]["max_posts"])

            path.write_text(json.dumps({
                "items": [{"code": "688981"}, {"code": "688981"}],
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "重复标的"):
                dm.load_config(path)

    def test_run_returns_nonzero_when_every_target_fails(self) -> None:
        failed = result("eastmoney", error="network down")
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(dm, "DATA_DIR", Path(tmp)), \
             mock.patch.object(dm, "load_config", return_value={
                 "items": [{"code": "688981", "type": "stock", "name": "中芯国际"}],
             }), \
             mock.patch.object(dm, "fetch_item_with_policy", return_value=[failed]):
            exit_code = dm.run(self.day, Path("unused.json"), source="auto", slot="测试")

        self.assertEqual(1, exit_code)

    @property
    def day(self) -> dt.date:
        return dt.date(2026, 8, 1)


if __name__ == "__main__":
    unittest.main()

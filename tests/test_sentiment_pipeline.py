import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sentiment_models import SentimentReport, SentimentValidationError, panic_band
from sentiment_report import render_markdown
from sentiment_store import DuplicateRunError, SentimentStore


def report_dict(*, panic: float = 62, quote: str = "谨慎观望，暂时不追高") -> dict:
    return {
        "schema_version": 1,
        "run": {
            "trade_date": "2026-08-01",
            "slot": "尾盘",
            "generated_at": "2026-08-01T15:20:00+08:00",
            "market_context": "大盘震荡分化，科技板块活跃。",
            "model": "test-model",
            "source_manifest": "data/fetch_manifest_2026-08-01_尾盘.json",
        },
        "items": [
            {
                "code": "688981",
                "name": "中芯国际",
                "security_type": "stock",
                "panic_index": panic,
                "confidence": 0.82,
                "sample_count": 105,
                "summary": "高位分歧增加，但尚未出现集中割肉。",
                "evidence": [
                    {"post_id": "p1", "source": "eastmoney", "quote": quote},
                ],
            },
        ],
    }


class SentimentModelTests(unittest.TestCase):
    def test_template_and_derived_mean_are_valid(self) -> None:
        template = Path(__file__).resolve().parents[1] / "SENTIMENT_RESULT_TEMPLATE.json"
        report = SentimentReport.from_path(template)

        self.assertEqual(-0.24, report.items[0].mean_sentiment)
        self.assertEqual("偏恐慌", panic_band(report.items[0].panic_index))

    def test_inconsistent_mean_sentiment_is_rejected(self) -> None:
        raw = report_dict()
        raw["items"][0]["mean_sentiment"] = 0.8

        with self.assertRaisesRegex(SentimentValidationError, "自然映射不一致"):
            SentimentReport.from_dict(raw)

    def test_duplicate_items_are_rejected(self) -> None:
        raw = report_dict()
        raw["items"].append(dict(raw["items"][0]))

        with self.assertRaisesRegex(SentimentValidationError, "重复标的"):
            SentimentReport.from_dict(raw)

    def test_fractional_sample_count_is_rejected(self) -> None:
        raw = report_dict()
        raw["items"][0]["sample_count"] = 10.5

        with self.assertRaisesRegex(SentimentValidationError, "类型无效"):
            SentimentReport.from_dict(raw)

    def test_markdown_uses_grouped_list_not_table(self) -> None:
        raw = report_dict()
        calm = dict(raw["items"][0])
        calm.update({"code": "600519", "name": "贵州茅台", "panic_index": 20})
        raw["items"].append(calm)
        markdown = render_markdown(
            SentimentReport.from_dict(raw),
            {"688981": {"pct_chg": 4.59, "return_5d_pct": 8.2, "volume_ratio_5d": 1.4}},
        )

        self.assertIn("🟢 亢奋/乐观", markdown)
        self.assertIn("🟡 纠结/偏谨慎", markdown)
        self.assertLess(markdown.index("贵州茅台"), markdown.index("中芯国际"))
        self.assertNotIn("|---", markdown)
        self.assertIn("当日 +4.59% / 5日 +8.20% / 量比 1.40x", markdown)


class SentimentStoreTests(unittest.TestCase):
    def test_save_history_duplicate_guard_and_replace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.db"
            store = SentimentStore(path)
            first = SentimentReport.from_dict(report_dict(quote="旧证据"))
            run_id = store.save_report(first)
            self.assertGreater(run_id, 0)

            with self.assertRaises(DuplicateRunError):
                store.save_report(first)

            replacement_raw = report_dict(panic=75, quote="新证据")
            replacement = SentimentReport.from_dict(replacement_raw)
            store.save_report(replacement, replace=True)
            history = store.history("688981")

            self.assertEqual(1, len(history))
            self.assertEqual(75, history[0]["panic_index"])
            with sqlite3.connect(path) as connection:
                quotes = [row[0] for row in connection.execute(
                    "SELECT quote FROM sentiment_evidence"
                ).fetchall()]
            self.assertEqual(["新证据"], quotes)

    def test_history_limit_must_be_positive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SentimentStore(Path(tmp) / "history.db")
            with self.assertRaisesRegex(ValueError, "正整数"):
                store.history("688981", limit=0)


if __name__ == "__main__":
    unittest.main()

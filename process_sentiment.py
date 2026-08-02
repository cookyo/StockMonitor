#!/usr/bin/env python3
"""校验结构化情绪 JSON，写入 SQLite，并生成 Markdown 日报。"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

from sentiment_models import SentimentReport, SentimentValidationError
from sentiment_report import render_markdown
from sentiment_store import DEFAULT_DB_PATH, DuplicateRunError, SentimentStore

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"


def safe_slot(slot: str) -> str:
    value = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", slot).strip("_")
    return value or "unknown"


def default_output_path(report: SentimentReport) -> Path:
    return DATA_DIR / (
        f"llm_report_{report.trade_date.isoformat()}_{safe_slot(report.slot)}.md"
    )


def write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def load_market_snapshots(report: SentimentReport, input_path: Path) -> dict[str, dict]:
    """从抓取 manifest 读取轻量量价事实；缺失时安静降级为纯情绪报告。"""
    if not report.source_manifest:
        return {}
    raw_path = Path(report.source_manifest)
    candidates = [raw_path] if raw_path.is_absolute() else [BASE_DIR / raw_path, input_path.parent / raw_path.name]
    manifest_path = next((path for path in candidates if path.exists()), None)
    if manifest_path is None:
        return {}
    try:
        rows = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(rows, list):
        return {}
    snapshots: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "")
        market = row.get("market")
        if code and isinstance(market, dict) and market:
            snapshots.setdefault(code, market)
    return snapshots


def main() -> int:
    parser = argparse.ArgumentParser(
        description="校验情绪 JSON -> 保存 SQLite 历史 -> 生成飞书 Markdown",
    )
    parser.add_argument("input", help="结构化情绪 JSON 文件")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite 历史库路径")
    parser.add_argument("--output", default="", help="Markdown 输出路径；默认写入 data/")
    parser.add_argument("--replace", action="store_true", help="显式替换同日期/时段历史")
    parser.add_argument("--no-store", action="store_true", help="只校验和渲染，不写 SQLite")
    args = parser.parse_args()

    try:
        input_path = Path(args.input)
        report = SentimentReport.from_path(input_path)
        markdown = render_markdown(report, load_market_snapshots(report, input_path))
        if not args.no_store:
            run_id = SentimentStore(Path(args.db)).save_report(report, replace=args.replace)
            print(f"[历史库] run_id={run_id} -> {args.db}")
        output = Path(args.output) if args.output else default_output_path(report)
        write_text_atomic(output, markdown)
    except SentimentValidationError as exc:
        print(f"结果校验失败: {exc}", file=sys.stderr)
        return 2
    except DuplicateRunError as exc:
        print(f"历史写入失败: {exc}", file=sys.stderr)
        return 3
    except (sqlite3.Error, RuntimeError, ValueError) as exc:
        print(f"历史库处理失败: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"文件写入失败: {exc}", file=sys.stderr)
        return 1

    print(f"[日报] {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

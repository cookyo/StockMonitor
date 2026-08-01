#!/usr/bin/env python3
"""校验结构化情绪 JSON，写入 SQLite，并生成 Markdown 日报。"""

from __future__ import annotations

import argparse
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
        report = SentimentReport.from_path(Path(args.input))
        markdown = render_markdown(report)
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

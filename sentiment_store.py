#!/usr/bin/env python3
"""SQLite 情绪历史库。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from sentiment_models import SentimentReport

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "sentiment_history.db"
DB_SCHEMA_VERSION = 1


class DuplicateRunError(RuntimeError):
    """同一交易日和时段已经存在结果，且调用方未明确允许替换。"""


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sentiment_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT NOT NULL,
    slot TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    market_context TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    source_manifest TEXT NOT NULL DEFAULT '',
    result_schema_version INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(trade_date, slot)
);

CREATE TABLE IF NOT EXISTS sentiment_results (
    run_id INTEGER NOT NULL REFERENCES sentiment_runs(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    security_type TEXT NOT NULL,
    panic_index REAL NOT NULL CHECK(panic_index BETWEEN 0 AND 100),
    confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
    sample_count INTEGER NOT NULL CHECK(sample_count >= 0),
    mean_sentiment REAL NOT NULL CHECK(mean_sentiment BETWEEN -1 AND 1),
    summary TEXT NOT NULL,
    PRIMARY KEY(run_id, code, security_type)
);

CREATE TABLE IF NOT EXISTS sentiment_evidence (
    run_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    security_type TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    post_id TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    quote TEXT NOT NULL,
    PRIMARY KEY(run_id, code, security_type, ordinal),
    FOREIGN KEY(run_id, code, security_type)
        REFERENCES sentiment_results(run_id, code, security_type) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sentiment_results_code
    ON sentiment_results(code, security_type);
CREATE INDEX IF NOT EXISTS idx_sentiment_runs_date
    ON sentiment_runs(trade_date, slot);
"""


class SentimentStore:
    def __init__(self, path: Path = DEFAULT_DB_PATH):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            current = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current > DB_SCHEMA_VERSION:
                raise RuntimeError(
                    f"数据库版本 {current} 高于程序支持版本 {DB_SCHEMA_VERSION}"
                )
            connection.executescript(SCHEMA_SQL)
            connection.execute(f"PRAGMA user_version = {DB_SCHEMA_VERSION}")

    def save_report(self, report: SentimentReport, replace: bool = False) -> int:
        self.initialize()
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT id FROM sentiment_runs WHERE trade_date = ? AND slot = ?",
                (report.trade_date.isoformat(), report.slot),
            ).fetchone()
            if existing is not None:
                if not replace:
                    raise DuplicateRunError(
                        f"{report.trade_date.isoformat()} / {report.slot} 已存在；"
                        "确认重跑时请显式使用 --replace"
                    )
                connection.execute("DELETE FROM sentiment_runs WHERE id = ?", (existing["id"],))

            cursor = connection.execute(
                """
                INSERT INTO sentiment_runs(
                    trade_date, slot, generated_at, market_context, model,
                    source_manifest, result_schema_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report.trade_date.isoformat(), report.slot, report.generated_at,
                    report.market_context, report.model, report.source_manifest,
                    report.schema_version,
                ),
            )
            run_id = int(cursor.lastrowid)
            for item in report.items:
                connection.execute(
                    """
                    INSERT INTO sentiment_results(
                        run_id, code, name, security_type, panic_index, confidence,
                        sample_count, mean_sentiment, summary
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id, item.code, item.name, item.security_type,
                        item.panic_index, item.confidence, item.sample_count,
                        item.mean_sentiment, item.summary,
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO sentiment_evidence(
                        run_id, code, security_type, ordinal, post_id, source, quote
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            run_id, item.code, item.security_type, ordinal,
                            evidence.post_id, evidence.source, evidence.quote,
                        )
                        for ordinal, evidence in enumerate(item.evidence)
                    ],
                )
            return run_id

    def history(
        self, code: str, security_type: str | None = None, limit: int = 30,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            raise ValueError("limit 必须是正整数")
        self.initialize()
        params: list[Any] = [code]
        type_clause = ""
        if security_type:
            type_clause = " AND r.security_type = ?"
            params.append(security_type)
        params.append(limit)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT u.trade_date, u.slot, u.generated_at,
                       r.code, r.name, r.security_type, r.panic_index,
                       r.confidence, r.sample_count, r.mean_sentiment, r.summary
                  FROM sentiment_results r
                  JOIN sentiment_runs u ON u.id = r.run_id
                 WHERE r.code = ?{type_clause}
                 ORDER BY u.trade_date DESC, u.generated_at DESC
                 LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description="管理 SQLite 情绪历史库")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite 文件路径")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", help="初始化数据库")
    history_parser = subparsers.add_parser("history", help="查询单个标的历史")
    history_parser.add_argument("--code", required=True)
    history_parser.add_argument("--type", choices=("stock", "etf", "index"), default=None)
    history_parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()

    store = SentimentStore(Path(args.db))
    if args.command == "init":
        store.initialize()
        print(f"已初始化: {store.path}")
        return 0
    rows = store.history(args.code, security_type=args.type, limit=args.limit)
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

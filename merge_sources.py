#!/usr/bin/env python3
"""把百度 + 新浪(可扩展东财)同日评论聚合成单份判读语料。

动机
----
百度股市通(baidu_*, 来源含东财+雪球)与新浪股吧(sina_*)是**两批完全独立的
用户群**(实测跨源正文 0 重复)。合并后:
  - 样本加厚: 科技股上新浪常有 50-60 帖, 与百度互补(如中芯 78+25、兆易 76+58);
  - 覆盖互补: 百度独有(星网锐捷/万华), 新浪独有(510300), 合并后几乎全覆盖。

加权约定(与 LLM_SENTIMENT_RUBRIC.md v2 一致)
------------------------------------------------
两源统一**等权**: 百度 views/replies 恒为 0, 新浪虽带热度但与百度不可比,
为保证跨源可复现, 一律等权(一条帖一票)。合并输出不写热度权重, 判读时按
"一帖一票"估 mean_sentiment。

去重
----
同源内已由各自 fetch 脚本按 post_id 去重; 跨源用 normalize(body) 判重
(去空白 + 去 URL + 去 $代码$ 标签, 取前 40 字), 今日实测跨源 0 重复,
仍保留此防线以防偶发转帖。

产出
----
data/merged_{code}_{date}.json / .csv, 字段与单源一致并新增:
  - source: "baidu" / "sina"(标注每帖来自哪个源)
  - stock_code: 统一为**纯数字 code**(便于跨源 join, 与百度一致)

用法
----
    python3 merge_sources.py                       # 合并 data/ 下今日全部标的
    python3 merge_sources.py --date 2026-07-31      # 指定日期
    python3 merge_sources.py --code 688981          # 只合并单只
    python3 merge_sources.py --slot 早盘             # 只合并指定时段
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import date
from pathlib import Path

from fetch_comments import DATA_DIR

# 合并输出的列(在单源 schema 基础上补 source)。
COLUMNS = [
    "source", "stock_code", "post_id", "title", "author", "published_at",
    "last_updated_at", "views", "replies", "post_url", "body", "provider",
]

# 参与合并的源前缀; 顺序即优先级(靠前的源在跨源重复时保留)。
SOURCE_PREFIXES = ("baidu", "sina")

def pure_code(raw: str) -> str:
    """剥掉 sh/sz/of 等前缀, 返回纯数字 code 作为跨源 join key。"""
    return re.sub(r"\D", "", raw or "")

def _normalize_body(text: str) -> str:
    """跨源去重键: 去 URL、去 $代码$ 标签、去空白, 取前 40 字。"""
    t = text or ""
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"\$[^$]+\$", "", t)          # 去掉 $上证指数(SH000001)$ 这类标签
    t = re.sub(r"\s+", "", t)
    return t[:40]

def _discover(
    target_date: date,
    slot: str | None = None,
    data_dir: Path | None = None,
) -> dict[tuple[str, str], dict[str, str]]:
    """扫描源文件，按 ``(code, slot)`` 分组。

    同时兼容历史无时段文件和当前带时段文件。指定 slot 时只返回该时段，
    防止早盘与尾盘文件被错误混合。
    """
    root = Path(data_dir or DATA_DIR)
    index: dict[tuple[str, str], dict[str, str]] = {}
    day = target_date.isoformat()
    for src in SOURCE_PREFIXES:
        for path in root.glob(f"{src}_*_{day}*.json"):
            m = re.match(rf"{src}_(.+?)_{re.escape(day)}(?:_(.+))?\.json$", path.name)
            if not m:
                continue
            code = pure_code(m.group(1))
            file_slot = m.group(2) or ""
            if slot is not None and file_slot != slot:
                continue
            index.setdefault((code, file_slot), {})[src] = str(path)
    return index

def merge_one(code: str, files: dict[str, str]) -> list[dict]:
    """合并单只标的的多源帖子, 返回按发帖时间倒序的行。"""
    seen: set[str] = set()
    merged: list[dict] = []
    for src in SOURCE_PREFIXES:            # 按优先级遍历, baidu 先入则跨源重复时保 baidu
        path = files.get(src)
        if not path:
            continue
        posts = json.loads(Path(path).read_text(encoding="utf-8"))
        for post in posts:
            key = _normalize_body(post.get("body") or post.get("title") or "")
            if key and key in seen:
                continue                    # 跨源重复正文, 丢弃
            if key:
                seen.add(key)
            row = {c: post.get(c, "") for c in COLUMNS}
            row["source"] = src
            row["stock_code"] = code        # 统一纯数字
            if not row["provider"]:
                row["provider"] = "新浪" if src == "sina" else ""
            merged.append(row)
    merged.sort(key=lambda r: str(r.get("published_at") or ""), reverse=True)
    return merged

def save_merged(
    rows: list[dict], code: str, target_date: date, slot: str | None = None,
    output_dir: Path | None = None,
) -> tuple[Path, Path]:
    out = Path(output_dir or DATA_DIR)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"merged_{code}_{target_date.isoformat()}"
    if slot:
        stem = f"{stem}_{slot}"
    json_path = out / f"{stem}.json"
    csv_path = out / f"{stem}.csv"
    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return csv_path, json_path

def main() -> None:
    parser = argparse.ArgumentParser(description="聚合百度+新浪同日评论")
    parser.add_argument("--date", default=date.today().isoformat(), help="YYYY-MM-DD")
    parser.add_argument("--code", default=None, help="只合并单只纯数字 code")
    parser.add_argument("--slot", default=None, help="只合并指定时段；不传则各时段分别合并")
    args = parser.parse_args()

    target = date.fromisoformat(args.date)
    index = _discover(target, slot=args.slot)
    if args.code:
        pc = pure_code(args.code)
        index = {key: files for key, files in index.items() if key[0] == pc}

    if not any(index.values()):
        print(f"[merge] {target}: 未找到任何 baidu_/sina_ 源文件")
        return

    total = 0
    print(f"{'code':8}{'baidu':>7}{'sina':>7}{'跨源重复':>9}{'合并后':>8}")
    for code, slot in sorted(index):
        files = index[(code, slot)]
        nb = len(json.loads(Path(files["baidu"]).read_text(encoding="utf-8"))) if "baidu" in files else 0
        ns = len(json.loads(Path(files["sina"]).read_text(encoding="utf-8"))) if "sina" in files else 0
        rows = merge_one(code, files)
        dup = nb + ns - len(rows)
        total += len(rows)
        save_merged(rows, code, target, slot=slot or None)
        label = f"{code}/{slot}" if slot else code
        print(f"{label:8}{nb:>7}{ns:>7}{dup:>9}{len(rows):>8}")
    print(f"合并完成: {len(index)} 只, 共 {total} 帖 -> data/merged_*_{target.isoformat()}.json")

if __name__ == "__main__":
    main()

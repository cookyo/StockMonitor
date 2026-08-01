#!/usr/bin/env python3
"""每日情绪监控编排器(纯抓取): 读配置 -> 抓每个标的评论(带正文) -> 落 data/ -> 生成抓取清单。

一个入口串起抓取环节,避免脚本散落:

  1) 读 monitor_config.json 里 enabled 的每个 item
  2) 默认逐标的先抓东财；东财失败时才同时抓百度和新浪备用源
  3) 每个标的的完整评论落到 data/eastmoney_{symbol}_{date}.json
  4) 生成抓取清单 data/fetch_manifest_{date}.{json,md}(抓了哪些、各多少帖、当日涨跌)

情绪判读**不在本脚本内**: 由大模型直接读 data/ 下的完整评论文件,
按 LLM_SENTIMENT_RUBRIC.md 的固定绝对尺生成结构化 JSON，再由
process_sentiment.py 校验、保存历史并生成 Markdown。本脚本只负责抓取与清单。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError

import fetch_comments as fc
import fetch_sina as fs
import fetch_baidu as fb
import merge_sources as ms

try:
    import fetch_quotes as fq
    _HAS_QUOTES = True
except Exception:  # 行情为可选背景信息,失败不阻断抓取
    _HAS_QUOTES = False

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CONFIG_PATH = BASE_DIR / "monitor_config.json"
SOURCES = ("eastmoney", "baidu", "sina")

# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

def load_config(path: Path) -> dict:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise ValueError("配置根节点必须是 JSON object")
    defaults = cfg.get("defaults", {})
    raw_items = cfg.get("items", [])
    if not isinstance(defaults, dict) or not isinstance(raw_items, list):
        raise ValueError("defaults 必须是 object，items 必须是 array")
    if any(not isinstance(it, dict) for it in raw_items):
        raise ValueError("items 中每一项都必须是 object")
    items = [dict(it) for it in raw_items if it.get("enabled", True)]
    for it in items:
        for k, v in defaults.items():
            it.setdefault(k, v)
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(items, start=1):
        code = str(item.get("code") or "").strip()
        sec_type = item.get("type", "stock")
        if not code:
            raise ValueError(f"第 {index} 个启用 item 缺少 code")
        if sec_type not in ("stock", "etf", "index"):
            raise ValueError(f"{code} 的 type 无效: {sec_type!r}")
        key = (code, sec_type)
        if key in seen:
            raise ValueError(f"配置存在重复标的: {code}/{sec_type}")
        seen.add(key)
        for field in ("pages", "max_posts"):
            if field in item and (not isinstance(item[field], int) or item[field] <= 0):
                raise ValueError(f"{code} 的 {field} 必须是正整数")
        by_source = item.get("pages_by_source", {})
        if not isinstance(by_source, dict):
            raise ValueError(f"{code} 的 pages_by_source 必须是 object")
        for source, pages in by_source.items():
            if source not in SOURCES or not isinstance(pages, int) or pages <= 0:
                raise ValueError(f"{code} 的 pages_by_source.{source} 必须是已知源的正整数")
        if item.get("sort_by", "published") not in ("published", "comments"):
            raise ValueError(f"{code} 的 sort_by 只能是 published/comments")
        if "with_body" in item and not isinstance(item["with_body"], bool):
            raise ValueError(f"{code} 的 with_body 必须是 boolean")
    return {"defaults": defaults, "items": items}

# --------------------------------------------------------------------------
# 抓取
# --------------------------------------------------------------------------

def _resolve_pages(item: dict, source: str, mod) -> int:
    """按数据源取翻页页数。三源"一页多少条"不同(见 monitor_config 注释),
    故优先读 pages_by_source[source]; 缺则回退单值 pages; 再回退模块 DEFAULT_PAGES。"""
    by_source = item.get("pages_by_source")
    if isinstance(by_source, dict) and source in by_source:
        return by_source[source]
    if "pages" in item:
        return item["pages"]
    return mod.DEFAULT_PAGES

def fetch_item(item: dict, target: dt.date, source: str = "eastmoney",
               slot: str | None = None) -> dict:
    """抓单个 item, 落 data/, 返回抓取结果。source: eastmoney / sina / baidu。
    slot: 时段标记(如"早盘"/HHMM), 透传给落盘做文件名后缀, 免同日多次跑互相覆盖。"""
    sec_type = item.get("type", "stock")
    if source == "sina":
        symbol = fs.resolve_sina_name(item["code"], sec_type)
        mod = fs
    elif source == "baidu":
        symbol = fb.resolve_baidu_symbol(item["code"], sec_type)
        mod = fb
    else:
        symbol = fc.resolve_guba_symbol(item["code"], sec_type)
        mod = fc
    try:
        rows, pages = mod.fetch_posts(
            symbol, target,
            pages=_resolve_pages(item, source, mod),
            max_posts=item.get("max_posts", fc.DEFAULT_MAX_POSTS),
            sort_by=item.get("sort_by", "published"),
            date_filter=True,
        )
    except fc.SoftBanError as exc:
        # 限流/页面异常: 不写文件, 保留上一次抓到的好数据, 交给上层退避重试。
        return {"source": source, "symbol": symbol, "pages": 0, "posts": [], "path": "",
                "error": f"限流跳过: {exc}", "soft_ban": True}
    except (HTTPError, URLError, TimeoutError, ValueError, RuntimeError) as exc:
        return {"source": source, "symbol": symbol, "pages": 0, "posts": [], "path": "",
                "error": f"抓取失败: {exc}"}

    if not rows:
        # 抓到 0 帖: 不写文件, 避免把上次的好数据覆盖成空。
        return {"source": source, "symbol": symbol, "pages": pages, "posts": [], "path": "",
                "error": "", "empty_skipped": True}

    if item.get("with_body", True):
        if source == "sina":
            fs.fetch_bodies_sina(rows, delay=0.1, workers=4)
        elif source == "baidu":
            fb.fetch_bodies_baidu(rows)  # 百度正文随列表直出, 空操作
        else:
            fc.fetch_bodies(rows, delay=0.15, workers=4)

    if source == "sina":
        _csv_path, json_path = fs.save_rows_sina(rows, DATA_DIR, symbol, target, slot=slot)
    elif source == "baidu":
        _csv_path, json_path = fb.save_rows_baidu(rows, DATA_DIR, symbol, target, slot=slot)
    else:
        _csv_path, json_path = fc.save_rows(rows, DATA_DIR, symbol, target, slot=slot)
    return {"source": source, "symbol": symbol, "pages": pages, "posts": rows,
            "path": str(json_path), "error": ""}

def fetch_item_with_policy(
    item: dict, target: dt.date, source: str, slot: str,
) -> list[dict]:
    """按数据源策略抓单个标的，返回实际发生的请求结果。

    auto 模式下东财只要请求成功（有帖或明确为 0 帖）就不会触发备用源；
    仅东财出现限流/网络/解析错误时，才抓百度和新浪并生成同一时段的合并文件。
    """
    if source != "auto":
        return [fetch_item(item, target, source=source, slot=slot)]

    primary = fetch_item(item, target, source="eastmoney", slot=slot)
    if not primary["error"]:
        return [primary]

    backups = [
        fetch_item(item, target, source=backup, slot=slot)
        for backup in ("baidu", "sina")
    ]
    files = {result["source"]: result["path"] for result in backups if result["path"]}
    if files:
        try:
            rows = ms.merge_one(str(item["code"]), files)
            _csv_path, json_path = ms.save_merged(
                rows, str(item["code"]), target, slot=slot, output_dir=DATA_DIR,
            )
            for result in backups:
                if not result["error"]:
                    result["merged_path"] = str(json_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            for result in backups:
                if not result["error"]:
                    result["merge_error"] = f"备用源合并失败: {exc}"
    return [primary, *backups]

def _today_pct_chg(code: str, target: dt.date) -> float | None:
    """当日涨跌%,仅作为 LLM 判读的背景信息,取不到不影响抓取。"""
    if not _HAS_QUOTES:
        return None
    try:
        beg = (target - dt.timedelta(days=10)).isoformat()
        rows = fq.fetch_daily(code, beg, target.isoformat())
        for r in reversed(rows):
            if r["trade_date"] == target.isoformat():
                return r.get("pct_chg")
    except Exception:
        return None
    return None

# --------------------------------------------------------------------------
# 抓取清单
# --------------------------------------------------------------------------

def build_manifest_row(item: dict, fetched: dict, pct_chg: float | None) -> dict:
    return {
        "name": item.get("name", item["code"]),
        "code": item["code"],
        "type": item.get("type", "stock"),
        "source": fetched.get("source", "eastmoney"),
        "symbol": fetched["symbol"],
        "count": len(fetched["posts"]),
        "pages": fetched.get("pages", 0),
        "pct_chg": pct_chg if not fetched["error"] else None,
        "file": fetched.get("path", ""),
        "merged_file": fetched.get("merged_path", ""),
        "error": fetched["error"],
        "merge_error": fetched.get("merge_error", ""),
        "soft_ban": fetched.get("soft_ban", False),
    }

def render_manifest_md(rows: list[dict], target: dt.date, slot: str = "") -> str:
    ok = [r for r in rows if not r["error"] and r["count"] > 0]
    empty = [r for r in rows if not r["error"] and r["count"] == 0]
    banned = [r for r in rows if r.get("soft_ban")]
    fail = [r for r in rows if r["error"] and not r.get("soft_ban")]

    slot_tag = f" · {slot}" if slot else ""
    target_count = len({(r["code"], r["type"]) for r in rows})
    lines = [f"# 抓取清单 · {target.isoformat()}{slot_tag}",
             f"共 {target_count} 个标的 / {len(rows)} 次数据源请求，成功 {len(ok)} / 空 {len(empty)} / 限流 {len(banned)} / 失败 {len(fail)}",
             ""]
    for r in sorted(ok, key=lambda x: x["count"], reverse=True):
        pct = "" if r["pct_chg"] is None else f" | 当日{r['pct_chg']:+.2f}%"
        merged = f" · 合并 `{Path(r['merged_file']).name}`" if r.get("merged_file") else ""
        lines.append(f"- [{r['source']}] {r['name']}({r['code']}/{r['type']}) · {r['count']}帖{pct} · `{Path(r['file']).name}`{merged}")
    if empty:
        lines.append("")
        lines.append("**当日无匹配帖:** " + "、".join(f"[{r['source']}] {r['name']}({r['code']})" for r in empty))
    if banned:
        lines.append("")
        lines.append("**被限流(已跳过,保留旧数据):** " + "、".join(f"[{r['source']}] {r['name']}({r['code']})" for r in banned))
    if fail:
        lines.append("")
        lines.append("**抓取失败:** " + "、".join(f"[{r['source']}] {r['name']}({r['error']})" for r in fail))
    merge_fail = [r for r in rows if r.get("merge_error")]
    if merge_fail:
        lines.append("")
        lines.append("**备用源合并失败:** " + "、".join(f"{r['name']}({r['merge_error']})" for r in merge_fail))
    lines.append("")
    lines.append("> 完整评论(带正文)已落 data/;情绪判读由大模型读评论后按 LLM_SENTIMENT_RUBRIC.md 产出。")
    return "\n".join(lines)

# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def run(target: dt.date, config_path: Path, source: str = "auto",
        slot: str | None = None) -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        cfg = load_config(config_path)
    except (OSError, ValueError) as exc:
        print(f"配置读取失败: {exc}", file=sys.stderr)
        return 2
    if not cfg["items"]:
        print("配置里没有启用的 item", file=sys.stderr)
        return 2

    # slot 归一化一次: 传了用友好名, 没传用当前钟点 HHMM。整轮抓取共用同一后缀,
    # 保证同日多次跑(早/午/尾, 或"想起来就跑")各自独立成组、互不覆盖。
    slot_tag = fc.slot_suffix(slot)

    manifest = []
    successful_targets = 0
    for item in cfg["items"]:
        print(f"[抓取|{source}|{slot_tag}] {item.get('name', item['code'])} ({item['code']}/{item.get('type')})",
              file=sys.stderr)
        fetched_results = fetch_item_with_policy(item, target, source=source, slot=slot_tag)
        if any(not result["error"] for result in fetched_results):
            successful_targets += 1
            pct_chg = _today_pct_chg(item["code"], target)
        else:
            pct_chg = None
        manifest.extend(build_manifest_row(item, result, pct_chg) for result in fetched_results)

    md = render_manifest_md(manifest, target, slot=slot_tag)
    mf_json = DATA_DIR / f"fetch_manifest_{target.isoformat()}_{slot_tag}.json"
    mf_md = DATA_DIR / f"fetch_manifest_{target.isoformat()}_{slot_tag}.md"
    mf_json.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    mf_md.write_text(md, encoding="utf-8")

    print("\n" + md + "\n")
    print(f"[已写入] {mf_json}")
    print(f"[已写入] {mf_md}")
    print(f"[提示] 以清单 file/merged_file 为准，交给大模型按统一尺度判读。")
    if successful_targets == 0:
        print("[失败] 所有启用标的均未获得成功响应", file=sys.stderr)
        return 1
    return 0

def main() -> int:
    p = argparse.ArgumentParser(description="每日股吧评论抓取编排器(纯抓取,不打分)")
    p.add_argument("--date", default=dt.date.today().isoformat(), help="目标日期 YYYY-MM-DD")
    p.add_argument("--config", default=str(CONFIG_PATH), help="监控配置路径")
    p.add_argument(
        "--source", choices=("auto", "eastmoney", "sina", "baidu"), default="auto",
        help="auto: 东财主源失败后才抓百度+新浪；其余值用于强制单源诊断。",
    )
    p.add_argument(
        "--slot", default=None,
        help="时段标记, 用于同日多次抓取互不覆盖(如 早盘/午盘/尾盘)。"
             "不传则自动用当前钟点 HHMM。落盘/清单文件名会带此后缀。",
    )
    args = p.parse_args()

    try:
        target = dt.date.fromisoformat(args.date)
    except ValueError:
        print("日期格式应为 YYYY-MM-DD", file=sys.stderr)
        return 2

    return run(target, Path(args.config), source=args.source, slot=args.slot)

if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""抓取新浪股市汇(新浪股吧)公开帖子(支持 个股 / ETF / 指数)。

作为东方财富(fetch_comments.py)的备用数据源: 东财按 IP 限流软封时,
新浪股市汇是纯 Python 可直连的公开 JSON 接口,无 WAF、无登录、无验证码。

数据链路(全部公开接口,只读):
  1) 个股页 http://guba.sina.com.cn/?s=bar&name={name}  -> 正则提该吧 bid
  2) 列表接口 /api/?s=bar&bid={bid}&num=N               -> 帖子列表 JSON
  3) 正文接口 /api/?s=thread&bid={bid}&tid={tid}         -> 单帖正文 content

输出字段归一到与 fetch_comments.py 完全一致的 schema, 便于 LLM 用同一把
尺子判读, 也能被 save_rows 直接落盘。文件名前缀为 sina_ 以区分来源。

用法示例:
    python3 fetch_sina.py --code 002156                 # 个股
    python3 fetch_sina.py --code 510300 --type etf      # ETF(自动用 of 前缀)
    python3 fetch_sina.py --code 000001 --type index    # 指数

默认: 保留最新 200 条、带正文、写入 ./data/ 目录。
"""

from __future__ import annotations

import argparse
import html as html_mod
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# 复用东财脚本的常量与文本清洗, 保持两源产出结构一致。
from fetch_comments import DATA_DIR, DEFAULT_MAX_POSTS, SoftBanError, clean_text, slot_suffix

BASE_URL = "http://guba.sina.com.cn"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# 列表接口每页条数。指数吧置顶帖多, 20 会被过滤到 0, 取大一点更稳。
DEFAULT_NUM = 60
# 单标的最多翻多少页(每页 DEFAULT_NUM 条), 够覆盖当日发帖量。
DEFAULT_PAGES = 5

_BID_CACHE_PATH = DATA_DIR / "sina_bid_cache.json"

# --------------------------------------------------------------------------
# 标的 -> 新浪 name 解析(个股 / ETF / 指数)
# --------------------------------------------------------------------------

def resolve_sina_name(code: str, sec_type: str) -> str:
    """把代码解析成新浪股市汇 name 参数(带交易所前缀)。

    - 个股:  6 开头沪市 sh, 0/3 开头深市 sz, 4/8/92 开头北交所 bj。
    - ETF:   新浪基金统一用 of 前缀, 如 of510300。
    - 指数:  399 开头深市 sz, 其余(000 上证系 / 688 科创)沪市 sh。
    - 已带前缀的原样信任(转小写)。
    """
    raw = code.strip().lower()
    if raw.startswith(("sh", "sz", "bj", "of")) and raw[2:].isdigit():
        return raw
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return raw
    if sec_type == "etf":
        return f"of{digits}"
    if sec_type == "index":
        return f"{'sz' if digits.startswith('399') else 'sh'}{digits}"
    # stock
    head = digits[0]
    if digits[:2] in ("43", "83", "87", "88", "92"):
        exch = "bj"
    elif head == "6":
        exch = "sh"
    elif head in ("0", "3"):
        exch = "sz"
    else:
        exch = "sh"
    return f"{exch}{digits}"

# --------------------------------------------------------------------------
# 网络
# --------------------------------------------------------------------------

def fetch_text(url: str, referer: str = BASE_URL + "/", decode: str = "gb2312") -> str:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "gzip",
            "Referer": referer,
        },
    )
    with urlopen(request, timeout=20) as response:
        raw = response.read()
        if response.headers.get("Content-Encoding") == "gzip":
            import gzip

            raw = gzip.decompress(raw)
        return raw.decode(decode, errors="replace")

def fetch_json(url: str, referer: str) -> dict:
    """打 JSONP 接口, 剥壳成 dict。失败/非 JSON 抛 ValueError。"""
    txt = fetch_text(url, referer=referer, decode="utf-8")
    match = re.search(r"\{.*\}", txt, flags=re.S)
    if not match:
        raise ValueError(f"接口未返回 JSON: {txt[:80]!r}")
    return json.loads(match.group(0))

# --------------------------------------------------------------------------
# symbol -> bid 解析(带本地缓存)
# --------------------------------------------------------------------------

def _load_bid_cache() -> dict[str, str]:
    try:
        return json.loads(_BID_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

def _save_bid_cache(cache: dict[str, str]) -> None:
    _BID_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _BID_CACHE_PATH.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
    )

def resolve_sina_bid(name: str, use_cache: bool = True) -> str:
    """抓个股页, 正则提该股吧的 bid(板块 id)。结果本地缓存, 避免每天重解析。

    bid 是新浪股市汇每个吧的稳定主键(个股/指数/ETF 各一个), 极少变动。
    """
    cache = _load_bid_cache() if use_cache else {}
    if name in cache:
        return cache[name]

    page = fetch_text(f"{BASE_URL}/?s=bar&name={name}")
    # 页面被限流/不存在时体积很小且无 thread 链接。
    if len(page) < 3000 and "s=thread" not in page:
        raise SoftBanError(f"{name} 页面异常({len(page)}B), 无法解析 bid")

    # bid 在页面里高频出现(每条帖子链接都带), 取众数最稳。
    bids = re.findall(r"s=thread&bid=(\d+)&tid=\d+", page)
    if not bids:
        bids = re.findall(r'bid[=:"\']*\s*(\d{2,7})', page)
    if not bids:
        raise ValueError(f"{name} 页面未解析出 bid")
    from collections import Counter

    bid = Counter(bids).most_common(1)[0][0]
    cache[name] = bid
    if use_cache:
        _save_bid_cache(cache)
    return bid

# --------------------------------------------------------------------------
# 列表抓取(归一字段到东财 schema)
# --------------------------------------------------------------------------

def _normalize_post(item: dict, symbol: str) -> dict[str, str | int]:
    """新浪列表帖 -> 东财一致 schema。"""
    tid = str(item.get("tid") or "")
    bid = str(item.get("bid") or "")
    return {
        "stock_code": symbol,
        "post_id": tid,
        "title": clean_text(str(item.get("title") or "")),
        "author": clean_text(str(item.get("uname") or "")),
        "published_at": str(item.get("ctime") or ""),
        "last_updated_at": str(item.get("lastctime") or item.get("ctime") or ""),
        "views": int(item.get("views") or 0),
        "replies": int(item.get("reply") or 0),
        "post_url": str(item.get("url") or f"{BASE_URL}/?s=thread&bid={bid}&tid={tid}"),
        "_bid": bid,  # 内部用于取正文, 落盘前由 save 逻辑忽略
    }

def fetch_posts_sina(
    name: str,
    bid: str,
    target_date: date,
    pages: int = DEFAULT_PAGES,
    num: int = DEFAULT_NUM,
    max_posts: int = DEFAULT_MAX_POSTS,
    date_filter: bool = True,
) -> tuple[list[dict[str, str | int]], int]:
    """翻页抓列表, 按 ctime 过滤当日(可关), 去重后取最新 max_posts 条。

    返回 (rows, pages_scanned)。发帖时间倒序; 某页已无当日帖则提前停。
    """
    referer = f"{BASE_URL}/?s=bar&name={name}"
    rows_by_id: dict[str, dict[str, str | int]] = {}
    pages_scanned = 0
    target_prefix = target_date.isoformat()

    for page in range(pages):
        start = page * num
        url = f"{BASE_URL}/api/?s=bar&start={start}&num={num}&ot=jsonp&jsvar=x&bid={bid}"
        data = fetch_json(url, referer=referer)
        if data.get("ret") != 1:
            break
        posts = data.get("data", []) or []
        pages_scanned += 1

        page_hit = 0
        for item in posts:
            row = _normalize_post(item, name)
            if item.get("isTop"):  # 置顶帖不代表当日情绪, 跳过
                continue
            if date_filter and not row["published_at"].startswith(target_prefix):
                continue
            rows_by_id[row["post_id"]] = row
            page_hit += 1

        if len(rows_by_id) >= max_posts:
            break
        if not posts:
            break
        # 列表按时间倒序: 本页最旧帖已早于目标日, 后续页更旧, 提前停。
        if date_filter and posts:
            oldest = str(posts[-1].get("ctime") or "")
            if oldest and oldest[:10] < target_prefix:
                break

    rows = sorted(
        rows_by_id.values(),
        key=lambda r: (str(r["published_at"]), str(r["post_id"])),
        reverse=True,
    )[:max_posts]
    return rows, pages_scanned

# --------------------------------------------------------------------------
# 正文抓取(单帖 thread 接口)
# --------------------------------------------------------------------------

def fetch_one_body_sina(row: dict[str, str | int]) -> tuple[str, str]:
    bid = str(row.get("_bid") or "")
    tid = str(row.get("post_id") or "")
    if not bid or not tid:
        return "", "缺少 bid/tid"
    url = f"{BASE_URL}/api/?s=thread&bid={bid}&tid={tid}&ot=jsonp&jsvar=x"
    try:
        data = fetch_json(url, referer=str(row.get("post_url") or BASE_URL))
        posts = data.get("data", []) or []
        if not posts:
            return "", "正文接口无数据"
        content = str(posts[0].get("content") or "")
        text = html_mod.unescape(re.sub(r"<[^>]+>", " ", content))
        return re.sub(r"[ \t]+", " ", text).strip(), ""
    except Exception as exc:  # 单帖失败不阻断整批
        return "", str(exc)

def fetch_bodies_sina(
    rows: list[dict[str, str | int]], delay: float = 0.1, workers: int = 4
) -> None:
    workers = max(1, min(workers, len(rows)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {}
        for index, row in enumerate(rows):
            if index and delay > 0:
                time.sleep(delay)
            futures[executor.submit(fetch_one_body_sina, row)] = index
        for completed, future in enumerate(as_completed(futures), start=1):
            index = futures[future]
            body, error = future.result()
            rows[index]["body"] = body
            if error:
                rows[index]["body_error"] = error
            if completed % 20 == 0 or completed == len(rows):
                print(f"正文 {completed}/{len(rows)}", file=sys.stderr)

# --------------------------------------------------------------------------
# 落盘(复用东财 save_rows, 但文件名前缀 sina_)
# --------------------------------------------------------------------------

def save_rows_sina(
    rows: list[dict[str, str | int]], output_dir: Path, name: str, target_date: date,
    slot: str | None = None,
) -> tuple[Path, Path]:
    # 落盘前剥掉内部字段 _bid。
    clean = [{k: v for k, v in row.items() if k != "_bid"} for row in rows]
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"sina_{name}_{target_date.isoformat()}"
    if slot is not None:
        stem = f"{stem}_{slot_suffix(slot)}"
    json_path = output_dir / f"{stem}.json"
    csv_path = output_dir / f"{stem}.csv"

    json_path.write_text(
        json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    import csv

    columns = [
        "stock_code", "post_id", "title", "author", "published_at",
        "last_updated_at", "views", "replies", "post_url",
    ]
    if any("body" in row for row in clean):
        columns.extend(["body", "body_error"])
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(clean)
    return csv_path, json_path

# --------------------------------------------------------------------------
# 编排入口(供 daily_monitor 调用, 签名对齐 fetch_comments.fetch_posts)
# --------------------------------------------------------------------------

def fetch_posts(
    symbol_or_name: str,
    target_date: date,
    pages: int = DEFAULT_PAGES,
    max_posts: int = DEFAULT_MAX_POSTS,
    sort_by: str = "published",  # 兼容签名; 新浪按发帖时间, comments 无独立排序
    date_filter: bool = True,
) -> tuple[list[dict[str, str | int]], int]:
    """与 fetch_comments.fetch_posts 同签名的适配入口。

    symbol_or_name 应为已解析的新浪 name(sh600519 / of510300 / sz399006)。
    """
    bid = resolve_sina_bid(symbol_or_name)
    return fetch_posts_sina(
        symbol_or_name, bid, target_date,
        pages=pages, num=DEFAULT_NUM, max_posts=max_posts, date_filter=date_filter,
    )

def main() -> int:
    parser = argparse.ArgumentParser(
        description="抓取新浪股市汇公开帖子(个股 / ETF / 指数), 东财备用源。"
    )
    parser.add_argument("--code", default="002156", help="标的代码, 如 002156 / 510300 / 000001")
    parser.add_argument(
        "--type", choices=("stock", "etf", "index"), default="stock",
        help="标的类型, 用于自动补前缀(ETF 用 of, 指数按 399 分深沪)",
    )
    parser.add_argument("--date", default=date.today().isoformat(), help="目标日期 YYYY-MM-DD")
    parser.add_argument("--all-dates", action="store_true", help="不按日期过滤, 取最新 N 条")
    parser.add_argument("--pages", type=int, default=DEFAULT_PAGES, help=f"翻页数, 默认 {DEFAULT_PAGES}")
    parser.add_argument("--num", type=int, default=DEFAULT_NUM, help=f"每页条数, 默认 {DEFAULT_NUM}")
    parser.add_argument("--max-posts", type=int, default=DEFAULT_MAX_POSTS, help="保留条数上限")
    parser.add_argument("--no-body", action="store_true", help="不抓正文")
    parser.add_argument("--output-dir", default=str(DATA_DIR), help="输出目录")
    parser.add_argument("--delay", type=float, default=0.1, help="正文请求间隔秒")
    parser.add_argument("--workers", type=int, default=4, help="正文并发数")
    args = parser.parse_args()

    try:
        target_date = date.fromisoformat(args.date)
    except ValueError:
        print("日期格式应为 YYYY-MM-DD", file=sys.stderr)
        return 2

    name = resolve_sina_name(args.code, args.type)
    date_filter = not args.all_dates

    try:
        bid = resolve_sina_bid(name)
        rows, pages_scanned = fetch_posts_sina(
            name, bid, target_date,
            pages=args.pages, num=args.num, max_posts=args.max_posts,
            date_filter=date_filter,
        )
    except SoftBanError as exc:
        print(f"页面异常: {exc}", file=sys.stderr)
        return 1
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        print(f"抓取失败: {exc}", file=sys.stderr)
        return 1

    if not args.no_body and rows:
        fetch_bodies_sina(rows, max(0.0, args.delay), max(1, args.workers))

    csv_path, json_path = save_rows_sina(rows, Path(args.output_dir), name, target_date)

    print(f"标的: {args.code} ({args.type}) -> 新浪 name: {name} (bid={bid})")
    print(f"日期: {target_date.isoformat()}" + ("(不过滤, 取最新 N 条)" if not date_filter else ""))
    print(f"翻页: {pages_scanned} 页 x {args.num} 条")
    print(f"最新发帖: {len(rows)} 条(上限 {args.max_posts})")
    print(f"正文: {'否' if args.no_body else '是'}")
    print(f"CSV:  {csv_path}")
    print(f"JSON: {json_path}")
    print("说明: 抓取新浪股市汇公开帖子; 正文为主帖内容, 不含楼中回复。")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""抓取百度股市通"股票讨论"公开帖子(支持 个股 / ETF / 指数)。

作为东方财富(fetch_comments.py)与新浪(fetch_sina.py)之外的第三个备用源。
百度股市通本身是**聚合器**: 讨论流里的帖子 provider 同时来自
东方财富 / 雪球 / 百度股市通, 因此它间接把东财、雪球的股吧评论汇拢过来,
在东财被 IP 软封、雪球被 WAF 拦截时仍能纯 Python 直连拿到评论。

数据链路(单一公开 JSON 接口, 只读, 无 WAF、无登录、无验证码):
  GET https://finance.pae.baidu.com/vapi/v1/stocktalklist?code={code}&market=ab&rn={rn}&pn={pn}

接口特性(与东财/新浪不同, 已实测):
  - code 用**纯数字**, market 统一用 `ab`(A股); 个股/指数/ETF 都走 ab。
  - 正文**直接在列表项 content.items 里**, 无需二次请求正文页 ->
    请求量比东财/新浪低一个量级, 几乎不会触发限流。
  - pn 是**步长≈1 的滑动窗口**(不是页偏移): pn 每 +1, 窗口只向更旧滑一条。
    因此需翻多页 + 按 comment_id 去重, "连续无新增即停"。
  - 只暴露最近一小段讨论流(约数十条当日帖), 样本量介于东财与新浪之间。

输出字段归一到与 fetch_comments.py 完全一致的 schema, 便于 LLM 用同一把
尺子判读。文件名前缀 baidu_ 以区分来源。

用法示例:
    python3 fetch_baidu.py --code 002396                # 个股 星网锐捷
    python3 fetch_baidu.py --code 510300 --type etf     # ETF
    python3 fetch_baidu.py --code 000001 --type index   # 指数(上证)

默认: 保留最新 200 条、写入 ./data/ 目录(正文随列表直出, 无需 --no-body)。
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import time
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# 复用东财脚本的常量与文本清洗, 保持三源产出结构一致。
from fetch_comments import DATA_DIR, DEFAULT_MAX_POSTS, SoftBanError, clean_text, slot_suffix

GATEWAY = "https://finance.pae.baidu.com"
LIST_PATH = "/vapi/v1/stocktalklist"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# 每次请求条数。取大一点(20)避开小窗口被置顶/过滤到 0 的老毛病。
DEFAULT_RN = 20
# 最多翻多少个滑动窗口。步长≈1, 取大以尽量吃干当日流; 靠"连续无新增"提前停。
DEFAULT_PAGES = 60
# 连续多少页无新增即判定流已到底, 停止翻页。
_STALE_STOP = 2
# 单次请求的退避重试次数与基准间隔(秒)。批量高频翻页时百度会临时返回
# HTTP 500 或 HTML 验证页, 短暂退避后即恢复, 故重试而非直接判失败。
_RETRY = 3
_RETRY_BACKOFF = 0.8
# 翻窗口之间的礼貌间隔(秒), 降低批量抓取触发临时限流的概率。
_PAGE_DELAY = 0.15

# --------------------------------------------------------------------------
# 标的 -> 百度 (code, market) 解析(个股 / ETF / 指数)
# --------------------------------------------------------------------------

def resolve_baidu_symbol(code: str, sec_type: str) -> str:
    """把用户输入解析成百度讨论接口用的纯数字 code。

    百度统一用 6 位数字 code + market=ab, 不区分交易所前缀(个股/指数/ETF 皆然),
    因此这里只需剥出数字。已带 sh/sz/bj 等前缀的一律去前缀取数字。
    """
    digits = re.sub(r"\D", "", code.strip().lower())
    return digits or code.strip()

def resolve_baidu_market(code: str, sec_type: str) -> str:
    """百度市场标签。

    个股 / 指数统一用 `ab`(A股大盘讨论域); ETF 必须用交易所域(实测 510300 在
    ab 域返 0, 在 sh 域才有帖): 沪市 ETF(5 开头)-> sh, 深市 ETF(1 开头)-> sz。
    """
    if sec_type == "etf":
        digits = re.sub(r"\D", "", code.strip().lower())
        return "sz" if digits.startswith("1") else "sh"
    return "ab"

# --------------------------------------------------------------------------
# 网络
# --------------------------------------------------------------------------

def fetch_json(code: str, market: str, rn: int, pn: int) -> dict:
    """打讨论列表接口, 返回解析后的 dict。

    批量高频翻页时百度会临时返回 HTTP 500 或 HTML 验证页(非 JSON), 短暂退避后
    即恢复。故对这两类瞬时限流做有限次退避重试; 重试仍失败才抛 ValueError。
    """
    url = f"{GATEWAY}{LIST_PATH}?code={code}&market={market}&rn={rn}&pn={pn}"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept-Encoding": "gzip",
        "Referer": f"https://finance.baidu.com/stock/{market}-{code}",
    }
    last_err = ""
    for attempt in range(_RETRY):
        try:
            request = Request(url, headers=headers)
            with urlopen(request, timeout=20) as response:
                raw = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
            text = raw.decode("utf-8", errors="replace")
        except HTTPError as exc:
            # 5xx 多为瞬时限流, 退避重试; 4xx 是确定性错误, 直接抛。
            if exc.code >= 500 and attempt < _RETRY - 1:
                last_err = f"HTTP {exc.code}"
                time.sleep(_RETRY_BACKOFF * (attempt + 1))
                continue
            raise
        stripped = text.strip()
        if not stripped:
            last_err = "空响应"
        elif stripped[0] in "{[":
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                last_err = f"JSON 解析失败: {stripped[:60]!r}"
        else:
            # HTML 验证/拦截页: 退避重试。
            last_err = f"非 JSON(疑似拦截页): {stripped[:60]!r}"
        if attempt < _RETRY - 1:
            time.sleep(_RETRY_BACKOFF * (attempt + 1))
    raise ValueError(f"接口异常 code={code} pn={pn}: {last_err}")

# --------------------------------------------------------------------------
# 列表解析(归一字段到东财 schema; 正文随列表直出)
# --------------------------------------------------------------------------

def _content_text(item: dict) -> str:
    """把 content.items(结构化富文本)拼成纯文本。

    item 形如 {"items": [{"type":"text","data":"..."}, {"type":"at",...}, ...]}。
    只取带 data 字符串的片段(text/at/topic/link 等), 拼接后清洗。
    """
    content = item.get("content")
    if isinstance(content, dict):
        parts = [
            str(seg.get("data", ""))
            for seg in content.get("items", []) or []
            if isinstance(seg, dict) and isinstance(seg.get("data"), str)
        ]
        return clean_text(" ".join(p for p in parts if p))
    if isinstance(content, str):
        return clean_text(content)
    return ""

def _normalize_post(item: dict, symbol: str) -> dict[str, str | int]:
    """百度讨论帖 -> 东财一致 schema。正文(body)随列表直接落位。"""
    author = item.get("author") or {}
    author_name = author.get("name", "") if isinstance(author, dict) else str(author)
    published = str(item.get("create_show_time") or "")
    post_url = str(item.get("real_loc") or item.get("third_url") or item.get("loc") or "")
    return {
        "stock_code": symbol,
        "post_id": str(item.get("comment_id") or item.get("thread_id") or ""),
        "title": "",  # 百度短帖无独立标题, 情绪信息全在正文
        "author": clean_text(str(author_name)),
        "published_at": published,
        "last_updated_at": published,
        "views": 0,  # 百度不提供阅读数
        "replies": int(item.get("reply_count") or 0),
        "post_url": post_url,
        "body": _content_text(item),  # 正文随列表直出, 无需二次请求
        "provider": str(item.get("provider") or ""),  # 来源(东方财富/雪球/百度股市通)
    }

def fetch_posts_baidu(
    symbol: str,
    market: str,
    target_date: date,
    pages: int = DEFAULT_PAGES,
    rn: int = DEFAULT_RN,
    max_posts: int = DEFAULT_MAX_POSTS,
    date_filter: bool = True,
) -> tuple[list[dict[str, str | int]], int]:
    """滑动窗口翻页抓讨论流, 按 comment_id 去重, 过滤当日, 取最新 max_posts 条。

    返回 (rows, pages_scanned)。发帖时间倒序; 连续无新增或翻到早于目标日则停。
    """
    rows_by_id: dict[str, dict[str, str | int]] = {}
    pages_scanned = 0
    stale = 0
    target_prefix = target_date.isoformat()

    for pn in range(pages):
        if pn:
            time.sleep(_PAGE_DELAY)  # 礼貌间隔, 降低批量翻页触发临时限流的概率
        data = fetch_json(symbol, market, rn, pn)
        if str(data.get("ResultCode")) != "0":
            # 首窗口就异常 -> 视作接口不可用/被拦, 交上层退避; 中途异常则停。
            if pn == 0:
                raise SoftBanError(
                    f"{symbol} 首窗口 ResultCode={data.get('ResultCode')}, 已退避"
                )
            break
        posts = ((data.get("Result") or {}).get("list")) or []
        if not posts:
            break
        pages_scanned += 1

        new_count = 0
        for item in posts:
            row = _normalize_post(item, symbol)
            pid = str(row["post_id"])
            if not pid or pid in rows_by_id:
                continue
            if date_filter and not str(row["published_at"]).startswith(target_prefix):
                continue
            rows_by_id[pid] = row
            new_count += 1

        if len(rows_by_id) >= max_posts:
            break
        # 滑动窗口连续无新增 -> 流已到底, 提前停。
        stale = stale + 1 if new_count == 0 else 0
        if stale >= _STALE_STOP:
            break
        # 时间倒序: 本窗口最旧帖已早于目标日, 更旧窗口无贡献, 提前停。
        if date_filter:
            oldest = str(posts[-1].get("create_show_time") or "")
            if oldest[:10] and oldest[:10] < target_prefix:
                break

    rows = sorted(
        rows_by_id.values(),
        key=lambda r: (str(r["published_at"]), str(r["post_id"])),
        reverse=True,
    )[:max_posts]
    return rows, pages_scanned

# --------------------------------------------------------------------------
# 正文抓取(百度正文随列表直出, 这里是空操作, 仅为与东财/新浪签名对齐)
# --------------------------------------------------------------------------

def fetch_bodies_baidu(rows: list[dict[str, str | int]], delay: float = 0.0, workers: int = 1) -> None:
    """百度列表已带正文(body), 无需二次抓取。保留同名函数以对齐编排层调用。"""
    return None

# --------------------------------------------------------------------------
# 落盘(文件名前缀 baidu_)
# --------------------------------------------------------------------------

def save_rows_baidu(
    rows: list[dict[str, str | int]], output_dir: Path, symbol: str, target_date: date,
    slot: str | None = None,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"baidu_{symbol}_{target_date.isoformat()}"
    if slot is not None:
        stem = f"{stem}_{slot_suffix(slot)}"
    json_path = output_dir / f"{stem}.json"
    csv_path = output_dir / f"{stem}.csv"

    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    import csv

    columns = [
        "stock_code", "post_id", "title", "author", "published_at",
        "last_updated_at", "views", "replies", "post_url", "body", "provider",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return csv_path, json_path

# --------------------------------------------------------------------------
# 编排入口(供 daily_monitor 调用, 签名对齐 fetch_comments.fetch_posts)
# --------------------------------------------------------------------------

def _market_from_code(digits: str) -> str:
    """仅凭纯数字 code 推导百度 market(供 daily_monitor 统一分发, 不带 type 时用)。

    A 股 code 段无歧义: ETF 占 5xxxxx(沪)/1xxxxx(深), 个股为 6/0/3 开头,
    指数为 000/399/688。因此 5 开头 -> sh, 1 开头 -> sz, 其余(含指数)-> ab。
    """
    if digits.startswith("5"):
        return "sh"
    if digits.startswith("1"):
        return "sz"
    return "ab"

def fetch_posts(
    symbol: str,
    target_date: date,
    pages: int = DEFAULT_PAGES,
    max_posts: int = DEFAULT_MAX_POSTS,
    sort_by: str = "published",  # 兼容签名; 百度按发帖时间倒序, 无独立排序维度
    date_filter: bool = True,
) -> tuple[list[dict[str, str | int]], int]:
    """与 fetch_comments.fetch_posts 同签名的适配入口。

    symbol 应为已解析的纯数字 code(如 002396 / 510300 / 000001);
    market 由 code 段自动推导(个股/指数 ab, 沪 ETF sh, 深 ETF sz)。

    注意: 外部 pages 语义(东财/新浪的"页偏移", 每页数十条)与百度"步长≈1 的
    滑动窗口"不同。若直接透传 config 的小 pages(如 3), 百度只会滑 3 条 ->
    严重漏抓。故这里取 max(pages, DEFAULT_PAGES) 作窗口上限, 靠"连续无新增即停"
    自动收敛(冷门股很快停, 不会空翻), 确保吃干当日流。
    """
    return fetch_posts_baidu(
        symbol, _market_from_code(symbol), target_date,
        pages=max(pages, DEFAULT_PAGES), rn=DEFAULT_RN,
        max_posts=max_posts, date_filter=date_filter,
    )

def main() -> int:
    parser = argparse.ArgumentParser(
        description="抓取百度股市通股票讨论公开帖子(个股 / ETF / 指数), 东财/新浪之外的第三源。"
    )
    parser.add_argument("--code", default="002396", help="标的代码, 如 002396 / 510300 / 000001")
    parser.add_argument(
        "--type", choices=("stock", "etf", "index"), default="stock",
        help="标的类型(百度统一 market=ab, 该参数仅用于语义, 不影响解析)",
    )
    parser.add_argument("--date", default=date.today().isoformat(), help="目标日期 YYYY-MM-DD")
    parser.add_argument("--all-dates", action="store_true", help="不按日期过滤, 取最新 N 条")
    parser.add_argument("--pages", type=int, default=DEFAULT_PAGES, help=f"最多翻窗口数, 默认 {DEFAULT_PAGES}")
    parser.add_argument("--rn", type=int, default=DEFAULT_RN, help=f"每次请求条数, 默认 {DEFAULT_RN}")
    parser.add_argument("--max-posts", type=int, default=DEFAULT_MAX_POSTS, help="保留条数上限")
    parser.add_argument("--output-dir", default=str(DATA_DIR), help="输出目录")
    args = parser.parse_args()

    try:
        target_date = date.fromisoformat(args.date)
    except ValueError:
        print("日期格式应为 YYYY-MM-DD", file=sys.stderr)
        return 2

    symbol = resolve_baidu_symbol(args.code, args.type)
    market = resolve_baidu_market(args.code, args.type)
    date_filter = not args.all_dates

    try:
        rows, pages_scanned = fetch_posts_baidu(
            symbol, market, target_date,
            pages=args.pages, rn=args.rn, max_posts=args.max_posts,
            date_filter=date_filter,
        )
    except SoftBanError as exc:
        print(f"接口异常: {exc}", file=sys.stderr)
        return 1
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        print(f"抓取失败: {exc}", file=sys.stderr)
        return 1

    csv_path, json_path = save_rows_baidu(rows, Path(args.output_dir), symbol, target_date)

    providers = sorted({str(r.get("provider") or "") for r in rows if r.get("provider")})
    print(f"标的: {args.code} ({args.type}) -> 百度 code: {symbol} (market={market})")
    print(f"日期: {target_date.isoformat()}" + ("(不过滤, 取最新 N 条)" if not date_filter else ""))
    print(f"翻窗口: {pages_scanned} 次 x {args.rn} 条")
    print(f"最新发帖: {len(rows)} 条(上限 {args.max_posts})")
    print(f"来源分布: {', '.join(providers) if providers else '—'}")
    print(f"CSV:  {csv_path}")
    print(f"JSON: {json_path}")
    print("说明: 抓取百度股市通聚合讨论(含东财/雪球来源); 正文随列表直出, 不含楼中回复。")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

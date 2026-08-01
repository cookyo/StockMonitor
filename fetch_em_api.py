#!/usr/bin/env python3
"""东方财富股吧移动端 JSON API 备用通道(个股 / ETF / 指数)。

当 PC 网页端(fetch_comments.py 的 guba.eastmoney.com/list,*.html)被 IP 软封、
返回 ~2826B 空壳时,自动切到本模块。它命中的是股吧移动端(mguba)前端真正调用的
公开 JSON 接口,与 PC 端限流策略不同,软封期间通常仍可直连。

数据链路(单一公开 JSON 接口, 只读, 无 WAF、无登录、无验证码、无 MD5 签名):
  GET https://gbapi.eastmoney.com/webarticlelist/api/Article/WebArticleList
      ?code={bar_code}&p={page}&ps={ps}&sorttype=0
      &product=guba&plat=Wap&version=300&deviceid=1

关键实测(2026-07-31 逆向 mguba 前端 list.js 得到):
  - **product=guba&plat=Wap&version=300&deviceid=1 这组参数是必需的**:缺任意一个
    接口返回 rc=0 / me="系统繁忙, 请稍后再试[00003]"(反爬拒绝)。带齐即 rc=1。
  - code 用与 PC 端一致的 symbol: 个股裸 6 位(600036), 指数带 zssh/zssz 前缀,
    ETF 带 sh/sz 前缀(复用 fetch_comments.resolve_guba_symbol,零适配)。
  - **正文(post_content)随列表直出**, 无需二次抓正文页 -> 比 PC 端更省请求、更快。
  - 同时带 post_click_count(阅读)/ post_comment_count(回复), 保留热度权重。
  - p 是真实页偏移(非滑动窗口), 翻页无重叠。

输出字段归一到与 fetch_comments.py 完全一致的 schema, 便于 LLM 用同一把尺子判读。
落盘仍走 fetch_comments.save_rows -> 文件名前缀 eastmoney_(对下游透明:软封降级
后产出的文件名/结构与正常抓取完全相同, merge 与判读都无需感知)。

用法(一般不单独调用, 由 fetch_comments.fetch_posts 软封时自动降级):
    python3 fetch_em_api.py --code 600036
    python3 fetch_em_api.py --code 000688 --type index
"""

from __future__ import annotations

import argparse
import gzip
import json
import ssl
import sys
import time
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# 复用 PC 端的 symbol 解析、文本清洗、落盘与常量, 保证降级前后完全同构。
from fetch_comments import (
    DATA_DIR,
    DEFAULT_MAX_POSTS,
    DEFAULT_PAGES,
    clean_text,
    resolve_guba_symbol,
    save_rows,
)

API_URL = "https://gbapi.eastmoney.com/webarticlelist/api/Article/WebArticleList"
REFERER = "https://mguba.eastmoney.com/"
USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
)
# 反爬必需的固定参数组(缺一即 rc=0)。逆向自 mguba list.js。
_FIXED_PARAMS = {"product": "guba", "plat": "Wap", "version": "300", "deviceid": "1"}
_PS = 20          # 每页条数
_RETRY = 3        # 单页退避重试次数
_RETRY_BASE = 0.6

# macOS 的 python.org 解释器可能未接入系统钥匙串。优先使用 certifi 的可信 CA
# 包（若已安装），否则回退系统默认 CA；两条路径都保留证书与主机名校验。
try:
    import certifi

    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

def _get_json(code: str, page: int) -> dict:
    """请求一页, 返回解析后的 JSON envelope。网络异常按调用方约定上抛。"""
    params = {"code": code, "p": page, "ps": _PS, "sorttype": 0, **_FIXED_PARAMS}
    url = f"{API_URL}?{urlencode(params)}"
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "gzip",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": REFERER,
        },
    )
    last_exc: Exception | None = None
    for attempt in range(_RETRY):
        try:
            with urlopen(request, timeout=20, context=_SSL_CTX) as response:
                raw = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
            return json.loads(raw.decode("utf-8", errors="replace"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_exc = exc
            if attempt < _RETRY - 1:
                time.sleep(_RETRY_BASE * (attempt + 1))
    raise last_exc if last_exc else RuntimeError("unknown error")

def _to_row(item: dict, symbol: str) -> dict[str, str | int]:
    """把 API 单条 article 映射到统一 schema(与 fetch_comments 一致)。"""
    post_id = str(item.get("post_id") or "")
    user = item.get("post_user") or {}
    author = user.get("user_nickname", "") if isinstance(user, dict) else ""
    published_at = str(item.get("post_publish_time") or "")
    last_updated_at = str(item.get("post_last_time") or published_at)
    # 移动端帖多为纯内容(post_title 常为空), 用正文首句兜底标题, 保持可读。
    body = clean_text(item.get("post_content") or item.get("post_abstract") or "")
    title = clean_text(item.get("post_title") or "") or body[:40]
    post_url = f"https://guba.eastmoney.com/news,{symbol},{post_id}.html" if post_id else ""
    return {
        "stock_code": symbol,
        "post_id": post_id,
        "title": title,
        "author": clean_text(author),
        "published_at": published_at,
        "last_updated_at": last_updated_at,
        "views": int(item.get("post_click_count") or 0),
        "replies": int(item.get("post_comment_count") or 0),
        "post_url": post_url,
        "body": body,
    }

def fetch_posts(
    symbol: str,
    target_date: date,
    pages: int = DEFAULT_PAGES,
    max_posts: int = DEFAULT_MAX_POSTS,
    sort_by: str = "published",  # 兼容签名; API 已按发帖时间倒序, 无独立排序维度
    date_filter: bool = True,
) -> tuple[list[dict[str, str | int]], int]:
    """扫最多 pages 页, 按目标日过滤后返回 (rows, pages_scanned)。

    签名与 fetch_comments.fetch_posts 完全一致, 供软封降级时无缝替换。
    rc != 1 视为接口拒绝(反爬), 抛 RuntimeError 交上层处理, 不静默返回空。
    """
    rows_by_id: dict[str, dict[str, str | int]] = {}
    pages_scanned = 0
    day = target_date.isoformat()

    for page in range(1, max(1, pages) + 1):
        payload = _get_json(symbol, page)
        if payload.get("rc") != 1:
            if page == 1:
                raise RuntimeError(
                    f"{symbol} JSON API 被拒: rc={payload.get('rc')} "
                    f"me={payload.get('me')!r}(参数或反爬策略变化, 需重新逆向)"
                )
            break
        articles = payload.get("re") or []
        pages_scanned += 1
        if not articles:
            break

        page_had_target = False
        for item in articles:
            row = _to_row(item, symbol)
            if date_filter and not str(row["published_at"]).startswith(day):
                continue
            page_had_target = True
            rows_by_id[str(row["post_id"] or row["post_url"])] = row

        if len(rows_by_id) >= max_posts:
            break
        # 日期过滤模式: 该页已无目标日帖(更旧页只会更老), 提前停。
        if date_filter and page > 1 and not page_had_target:
            break

    rows = sorted(
        rows_by_id.values(),
        key=lambda r: (str(r["published_at"]), str(r.get("post_id", ""))),
        reverse=True,
    )[:max_posts]
    return rows, pages_scanned

def fetch_bodies(rows: list[dict[str, str | int]], delay: float = 0.0, workers: int = 1) -> None:
    """JSON API 列表已带正文(body), 无需二次抓取。保留同名函数以对齐编排层调用。"""
    return None

def main() -> int:
    parser = argparse.ArgumentParser(
        description="东方财富股吧移动端 JSON API 备用抓取(软封降级通道)。"
    )
    parser.add_argument("--code", default="600036", help="标的代码, 如 600036 / 510300 / 000688")
    parser.add_argument(
        "--type", choices=("stock", "etf", "index"), default="stock",
        help="标的类型, 用于自动补交易所前缀, 默认 stock",
    )
    parser.add_argument("--date", default=date.today().isoformat(), help="目标日期 YYYY-MM-DD")
    parser.add_argument("--all-dates", action="store_true", help="不按日期过滤, 取最新 N 条")
    parser.add_argument("--pages", type=int, default=DEFAULT_PAGES, help=f"最多扫描页数, 默认 {DEFAULT_PAGES}")
    parser.add_argument("--max-posts", type=int, default=DEFAULT_MAX_POSTS, help=f"保留最新条数, 默认 {DEFAULT_MAX_POSTS}")
    parser.add_argument("--output-dir", default=str(DATA_DIR), help=f"输出目录, 默认 {DATA_DIR}")
    args = parser.parse_args()

    try:
        target_date = date.fromisoformat(args.date)
    except ValueError:
        print("日期格式应为 YYYY-MM-DD, 例如 2026-07-31", file=sys.stderr)
        return 2

    symbol = resolve_guba_symbol(args.code, args.type)
    try:
        rows, pages_scanned = fetch_posts(
            symbol, target_date,
            pages=args.pages, max_posts=args.max_posts,
            date_filter=not args.all_dates,
        )
    except (HTTPError, URLError, TimeoutError, RuntimeError) as exc:
        print(f"抓取失败: {exc}", file=sys.stderr)
        return 1

    csv_path, json_path = save_rows(rows, Path(args.output_dir), symbol, target_date)
    print(f"标的: {args.code} ({args.type}) -> 股吧 symbol: {symbol}  [JSON API 备用通道]")
    print(f"日期: {target_date.isoformat()}" + ("（不过滤, 取最新 N 条）" if args.all_dates else ""))
    print(f"扫描页: {pages_scanned}  最新发帖: {len(rows)} 条（上限 {args.max_posts}）")
    print(f"CSV:  {csv_path}")
    print(f"JSON: {json_path}")
    print("说明: 正文随列表直出, 与 PC 端产出同构; 仅在东财软封时作为降级通道。")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

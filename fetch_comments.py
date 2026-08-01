#!/usr/bin/env python3
"""抓取东方财富股吧公开帖子（支持 个股 / ETF / 指数）。

只读取公开列表页与公开主帖正文，不登录、不逆向私有 API、不绕过验证码、
不抓取登录后或动态加载的楼中回复。是本项目唯一的股吧抓取入口。

用法示例:
    # 个股（裸 6 位代码）
    python3 fetch_comments.py --code 002156
    # ETF（自动补 sh/sz 前缀，也可直接传 sh588000）
    python3 fetch_comments.py --code 588000 --type etf
    # 指数（自动补 zssh/zssz 前缀，也可直接传 zssh000688）
    python3 fetch_comments.py --code 000688 --type index

默认: 扫 5 页、保留最新 200 条、带正文、写入 ./data/ 目录。
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

BASE_URL = "https://guba.eastmoney.com"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36"
DEFAULT_MAX_POSTS = 200
DEFAULT_PAGES = 5
DATA_DIR = Path(__file__).resolve().parent / "data"

# 东财限流(软封)时返回约 2826 字节的手机版 SPA 空壳:无 article 列表、标注 mobile。
# 低于此字节数且不含帖子列表迹象即判为软封,应退避而非当作"当日无帖"。
SOFT_BAN_MAX_BYTES = 5000

class SoftBanError(RuntimeError):
    """东财返回限流空壳(疑似 IP 软封)。调用方应退避重试,不要覆盖已有好数据。"""

def looks_soft_banned(page_html: str) -> bool:
    """列表页是否为限流空壳: 体积过小且不含帖子列表迹象。"""
    if len(page_html) >= SOFT_BAN_MAX_BYTES:
        return False
    return "article" not in page_html.lower()

# --------------------------------------------------------------------------
# 标的 -> 股吧 symbol 解析（个股 / ETF / 指数）
# --------------------------------------------------------------------------

def _guess_exchange(digits: str, sec_type: str) -> str:
    """按代码段推断交易所前缀。"""
    if sec_type == "index":
        # 深市指数为 399xxx（深证成指/创业板指）；其余（000xxx 上证系、688 科创）走沪市。
        return "sz" if digits.startswith("399") else "sh"
    # ETF: 深市 ETF 多为 15xxxx/16xxxx/18xxxx；沪市 ETF 为 5xxxxx。
    return "sz" if digits.startswith("1") else "sh"

def resolve_guba_symbol(code: str, sec_type: str) -> str:
    """把用户输入的代码解析成股吧列表页用的 symbol。

    - 个股: 裸 6 位代码即可，如 002156 / 600519。
    - ETF:  需 sh/sz 前缀，如 sh588000；传裸 588000 + --type etf 会自动补。
    - 指数: 需 zssh/zssz 前缀，如 zssh000688；传裸 000688 + --type index 会自动补。
    - 已带前缀的代码原样信任（转小写），作为自动推断的兜底逃生口。
    """
    raw = code.strip().lower()
    if raw.startswith(("zssh", "zssz")):
        return raw
    if raw.startswith(("sh", "sz")) and raw[2:].isdigit():
        return raw
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return raw
    if sec_type == "stock":
        return digits.zfill(6)
    exch = _guess_exchange(digits, sec_type)
    return f"zs{exch}{digits}" if sec_type == "index" else f"{exch}{digits}"

# --------------------------------------------------------------------------
# 网络与解析
# --------------------------------------------------------------------------

def clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()

def fetch(url: str) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    with urlopen(request, timeout=20) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")

def extract(pattern: str, fragment: str) -> str:
    match = re.search(pattern, fragment, flags=re.S | re.I)
    return clean_text(match.group(1)) if match else ""

def extract_json_object(page_html: str, marker: str) -> str:
    """Extract one embedded JSON object without relying on later script vars."""
    start = page_html.find(marker)
    if start < 0:
        return ""
    start += len(marker)
    while start < len(page_html) and page_html[start].isspace():
        start += 1
    if start >= len(page_html) or page_html[start] != "{":
        return ""

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(page_html)):
        char = page_html[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return page_html[start : index + 1]
    return ""

def parse_article_metadata(page_html: str) -> dict[str, dict[str, object]]:
    """Read publish/update timestamps from the public page's embedded article list."""
    payload_text = extract_json_object(page_html, "var article_list=")
    if not payload_text:
        return {}
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        return {}

    return {
        str(item.get("post_id")): item
        for item in payload.get("re", [])
        if item.get("post_id")
    }

def parse_rows(
    page_html: str,
    code: str,
    target_date: date,
    article_metadata: dict[str, dict[str, object]],
    sort_by: str,
    date_filter: bool = True,
) -> list[dict[str, str | int]]:
    """解析一页帖子。date_filter=False 时不按日期过滤（用于取"最新 N 条"）。"""
    rows: list[dict[str, str | int]] = []
    row_pattern = re.compile(r'<tr\s+class="listitem"[^>]*>(.*?)</tr>', re.S | re.I)

    for fragment in row_pattern.findall(page_html):
        update = extract(r'<div\s+class="update"[^>]*>(.*?)</div>', fragment)
        if not update:
            continue

        title_match = re.search(
            r'<div\s+class="title"[^>]*>\s*<a([^>]*)>(.*?)</a>',
            fragment,
            flags=re.S | re.I,
        )
        if not title_match:
            continue

        title_attrs, title_html = title_match.groups()
        post_id_match = re.search(r'data-postid="([^"]+)"', title_attrs, flags=re.I)
        href_match = re.search(r'href="([^"]+)"', title_attrs, flags=re.I)
        post_id = post_id_match.group(1) if post_id_match else ""
        href = href_match.group(1) if href_match else ""
        metadata = article_metadata.get(post_id, {})

        published_at = str(metadata.get("post_publish_time") or "")
        last_updated_at = str(metadata.get("post_last_time") or "")
        if not published_at:
            # Fallback for pages that do not expose embedded metadata.
            if re.match(r"^\d{2}-\d{2}\s", update):
                if date_filter and update[:5] != target_date.strftime("%m-%d"):
                    continue
                published_at = f"{target_date.year}-{update}"
            else:
                if date_filter and not update.startswith(target_date.isoformat()):
                    continue
                published_at = update
        if not last_updated_at:
            last_updated_at = published_at

        if date_filter:
            date_field = last_updated_at if sort_by == "comments" else published_at
            if not date_field.startswith(target_date.isoformat()):
                continue

        author_match = re.search(
            r'<div\s+class="author"[^>]*>.*?<a[^>]*>(.*?)</a>',
            fragment,
            flags=re.S | re.I,
        )

        rows.append(
            {
                "stock_code": code,
                "post_id": post_id,
                "title": clean_text(title_html),
                "author": clean_text(author_match.group(1)) if author_match else "",
                "published_at": published_at,
                "last_updated_at": last_updated_at,
                "views": int(extract(r'<div\s+class="read"[^>]*>(.*?)</div>', fragment) or 0),
                "replies": int(extract(r'<div\s+class="reply"[^>]*>(.*?)</div>', fragment) or 0),
                "post_url": urljoin(BASE_URL, href),
            }
        )

    return rows

def select_latest(
    rows: list[dict[str, str | int]],
    sort_by: str,
    max_posts: int = DEFAULT_MAX_POSTS,
) -> list[dict[str, str | int]]:
    deduped = {
        str(row.get("post_id") or row.get("post_url")): row
        for row in rows
    }
    ordered = list(deduped.values())
    time_field = "last_updated_at" if sort_by == "comments" else "published_at"
    ordered.sort(
        key=lambda row: (str(row[time_field]), str(row.get("post_id", ""))),
        reverse=True,
    )
    return ordered[:max_posts]

# --------------------------------------------------------------------------
# 抓取编排（单个标的，多页）
# --------------------------------------------------------------------------

def _fetch_posts_pc(
    symbol: str,
    target_date: date,
    pages: int = DEFAULT_PAGES,
    max_posts: int = DEFAULT_MAX_POSTS,
    sort_by: str = "published",
    date_filter: bool = True,
) -> tuple[list[dict[str, str | int]], int]:
    """PC 网页端抓取(默认通道)。软封时抛 SoftBanError, 由 fetch_posts 决定是否降级。"""
    rows_by_id: dict[str, dict[str, str | int]] = {}
    pages_scanned = 0
    for page_number in range(1, max(1, pages) + 1):
        list_url = (
            f"{BASE_URL}/list,{symbol}.html"
            if page_number == 1
            else f"{BASE_URL}/list,{symbol}_{page_number}.html"
        )
        page_html = fetch(list_url)
        if page_number == 1 and looks_soft_banned(page_html):
            raise SoftBanError(
                f"{symbol} 第一页疑似限流空壳({len(page_html)}B),已退避,不覆盖已有数据"
            )
        page_rows = parse_rows(
            page_html,
            symbol,
            target_date,
            parse_article_metadata(page_html),
            sort_by,
            date_filter=date_filter,
        )
        for row in page_rows:
            rows_by_id[str(row.get("post_id") or row.get("post_url"))] = row
        pages_scanned += 1

        if len(rows_by_id) >= max_posts:
            break
        # 日期过滤模式下：某页已无目标日帖子，后续更旧页不可能贡献，提前停。
        if date_filter and page_number > 1 and not page_rows:
            break

    rows = select_latest(list(rows_by_id.values()), sort_by, max_posts)
    return rows, pages_scanned

# 环境变量置 1 可禁用 JSON API 主通道, 强制只走 PC 网页端(诊断/对照用)。
_DISABLE_API_PRIMARY = os.environ.get("KH_DISABLE_EM_API_PRIMARY") == "1"

def fetch_posts(
    symbol: str,
    target_date: date,
    pages: int = DEFAULT_PAGES,
    max_posts: int = DEFAULT_MAX_POSTS,
    sort_by: str = "published",
    date_filter: bool = True,
) -> tuple[list[dict[str, str | int]], int]:
    """扫最多 pages 页，去重后按时间倒序取 max_posts 条。返回 (rows, pages_scanned)。

    **主通道: 东财移动端 JSON API(fetch_em_api)** —— 正文随列表直出, 请求量约为
    PC 端的 1/20, 更快、更抗软封。JSON API 被拒(rc=0, 反爬策略变化)或网络异常时,
    **自动回退 PC 网页端(_fetch_posts_pc)** 兜底; PC 端也软封才把 SoftBanError
    抛回上层, 沿用既有"退避跳过, 不覆盖旧数据"逻辑。两条通道产出结构与文件名完全
    同构, 对 daily_monitor / merge / 判读全程透明。

    置 KH_DISABLE_EM_API_PRIMARY=1 可跳过 JSON API, 强制只走 PC 端(诊断/对照用)。

    抛出网络异常给调用方处理（第一页失败通常意味着代码/前缀不对或被限流）。
    """
    if not _DISABLE_API_PRIMARY:
        try:
            import fetch_em_api
            return fetch_em_api.fetch_posts(
                symbol, target_date,
                pages=pages, max_posts=max_posts,
                sort_by=sort_by, date_filter=date_filter,
            )
        except (HTTPError, URLError, TimeoutError, RuntimeError) as exc:
            # JSON API 挂了(反爬变化/网络) -> 回退 PC 网页端兜底。
            print(f"[回退] {symbol} JSON API 不可用({exc}), 改走 PC 网页端", file=sys.stderr)

    # 兜底通道: PC 网页端。软封时抛 SoftBanError 交上层退避跳过。
    return _fetch_posts_pc(
        symbol, target_date,
        pages=pages, max_posts=max_posts,
        sort_by=sort_by, date_filter=date_filter,
    )

class BodyParser(HTMLParser):
    """Extract visible text from the main public post body."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.active_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        classes = set((attr_map.get("class") or "").split())
        is_post_body = "xeditor_content" in classes or attr_map.get("id") == "zw_body"
        if self.active_depth == 0 and is_post_body:
            self.active_depth = 1
            return
        if self.active_depth:
            self.active_depth += 1
            if tag in {"br", "p", "div", "li", "tr"}:
                self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.active_depth and tag in {"br", "img"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if not self.active_depth:
            return
        if tag in {"br", "p", "div", "li", "tr"}:
            self.parts.append("\n")
        self.active_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.active_depth:
            self.parts.append(data)

    def text(self) -> str:
        lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in self.parts]
        return "\n".join(line for line in lines if line).strip()

def extract_body(page_html: str) -> str:
    parser = BodyParser()
    parser.feed(page_html)
    return parser.text()

def fetch_one_body(row: dict[str, str | int]) -> tuple[str, str]:
    try:
        return extract_body(fetch(str(row.get("post_url", "")))), ""
    except Exception as exc:  # Keep one bad public page from aborting the batch.
        return "", str(exc)

def fetch_bodies(rows: list[dict[str, str | int]], delay: float, workers: int) -> None:
    # 已带正文的行(如 JSON API 降级通道直出的 body)跳过, 避免二次抓取覆盖成空。
    pending = [row for row in rows if not str(row.get("body") or "").strip()]
    if not pending:
        return
    workers = max(1, min(workers, len(pending)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {}
        for index, row in enumerate(pending):
            if index and delay > 0:
                time.sleep(delay)
            futures[executor.submit(fetch_one_body, row)] = row

        for completed, future in enumerate(as_completed(futures), start=1):
            row = futures[future]
            body, error = future.result()
            row["body"] = body
            if error:
                row["body_error"] = error
            if completed % 20 == 0 or completed == len(pending):
                print(f"正文 {completed}/{len(pending)}", file=sys.stderr)

# --------------------------------------------------------------------------
# 落盘
# --------------------------------------------------------------------------

def slot_suffix(slot: str | None) -> str:
    """把时段标记清洗成安全的文件名后缀。

    slot=None -> 用当前钟点 HHMM(保证"想起来就跑"任意次都不互相覆盖);
    传了 -> 允许"早盘/午盘/尾盘"这类友好名, 只保留中英文数字, 其余替换为下划线。
    """
    import re as _re
    from datetime import datetime as _dt
    raw = (slot or "").strip()
    if not raw:
        return _dt.now().strftime("%H%M")
    safe = _re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", raw).strip("_")
    return safe or _dt.now().strftime("%H%M")

def save_rows(
    rows: list[dict[str, str | int]],
    output_dir: Path,
    symbol: str,
    target_date: date,
    slot: str | None = None,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"eastmoney_{symbol}_{target_date.isoformat()}"
    if slot is not None:
        stem = f"{stem}_{slot_suffix(slot)}"
    json_path = output_dir / f"{stem}.json"
    csv_path = output_dir / f"{stem}.csv"

    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    columns = [
        "stock_code",
        "post_id",
        "title",
        "author",
        "published_at",
        "last_updated_at",
        "views",
        "replies",
        "post_url",
    ]
    if any("body" in row for row in rows):
        columns.extend(["body", "body_error"])
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    return csv_path, json_path

def main() -> int:
    parser = argparse.ArgumentParser(
        description="抓取东方财富股吧公开帖子（个股 / ETF / 指数）。"
    )
    parser.add_argument("--code", default="002156", help="标的代码，如 002156 / 588000 / 000688")
    parser.add_argument(
        "--type",
        choices=("stock", "etf", "index"),
        default="stock",
        help="标的类型，用于自动补交易所前缀，默认 stock",
    )
    parser.add_argument(
        "--date",
        default=date.today().isoformat(),
        help="目标日期 YYYY-MM-DD，默认今天；配合 --all-dates 可忽略日期过滤",
    )
    parser.add_argument(
        "--all-dates",
        action="store_true",
        help="不按日期过滤，直接取扫描到的最新 N 条（跨日）",
    )
    parser.add_argument(
        "--sort-by",
        choices=("comments", "published"),
        default="published",
        help="按最后回复(comments)或发帖时间(published)排序/过滤，默认 published",
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=DEFAULT_PAGES,
        help=f"最多扫描的列表页数，默认 {DEFAULT_PAGES}",
    )
    parser.add_argument(
        "--max-posts",
        type=int,
        default=DEFAULT_MAX_POSTS,
        help=f"保留的最新帖子条数，默认 {DEFAULT_MAX_POSTS}",
    )
    parser.add_argument(
        "--no-body",
        action="store_true",
        help="不抓正文（默认抓取主帖正文）",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DATA_DIR),
        help=f"输出目录，默认 {DATA_DIR}",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.15,
        help="提交正文页之间的间隔秒数，默认 0.15",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="正文抓取并发数，默认 4；设 1 为串行",
    )
    args = parser.parse_args()

    try:
        target_date = date.fromisoformat(args.date)
    except ValueError:
        print("日期格式应为 YYYY-MM-DD，例如 2026-07-31", file=sys.stderr)
        return 2

    symbol = resolve_guba_symbol(args.code, args.type)
    date_filter = not args.all_dates

    try:
        rows, pages_scanned = fetch_posts(
            symbol,
            target_date,
            pages=args.pages,
            max_posts=args.max_posts,
            sort_by=args.sort_by,
            date_filter=date_filter,
        )
    except (HTTPError, URLError, TimeoutError) as exc:
        print(f"下载失败: {exc}", file=sys.stderr)
        print(f"若为 ETF/指数，请确认已带正确前缀（sh/sz/zssh/zssz）或使用 --type。symbol={symbol}", file=sys.stderr)
        return 1

    if not args.no_body and rows:
        fetch_bodies(rows, max(0.0, args.delay), max(1, args.workers))

    csv_path, json_path = save_rows(rows, Path(args.output_dir), symbol, target_date)

    print(f"标的: {args.code} ({args.type}) -> 股吧 symbol: {symbol}")
    print(f"日期: {target_date.isoformat()}" + ("（不过滤，取最新 N 条）" if not date_filter else ""))
    print(f"扫描列表页: {pages_scanned} 页")
    label = "最新评论/更新" if args.sort_by == "comments" else "最新发帖"
    print(f"{label}: {len(rows)} 条（上限 {args.max_posts} 条）")
    print(f"正文: {'否' if args.no_body else '是'}")
    print(f"CSV:  {csv_path}")
    print(f"JSON: {json_path}")
    print("说明: 抓取公开帖子；正文为主帖内容，不含登录后或动态加载的楼中回复。")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

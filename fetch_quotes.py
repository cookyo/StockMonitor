#!/usr/bin/env python3
"""腾讯公开行情日线抓取(纯标准库,零 token)。

参考 light_stock2-ref-v1.34/fetchers/tx_stk_history_fetcher.py 的接口与解析,
但去掉 pandas/requests 依赖,改用标准库,便于 KongHuang 独立运行。

只读腾讯公开的前复权日线接口,不登录、不绕验证码。
"""

from __future__ import annotations

import json
import random
import time
import urllib.request
from urllib.error import HTTPError, URLError

URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
MAX_DAYS = 1600  # 单次拉取上限(交易日条数)

def to_tencent_code(code: str) -> str:
    """A股代码 -> 腾讯 symbol,如 002156 -> sz002156。"""
    raw = (code or "").strip().lower().replace(".", "").replace("_", "").replace("-", "")
    if raw.startswith(("sh", "sz", "bj")):
        digits = "".join(ch for ch in raw[2:] if ch.isdigit())
        if len(digits) != 6:
            raise ValueError(f"无法识别的代码: {code}")
        return raw[:2] + digits
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) != 6:
        raise ValueError(f"无法识别的代码: {code}")
    if digits.startswith(("00", "30", "12", "16", "39", "159")):
        return f"sz{digits}"
    if digits.startswith(("60", "68", "5", "9", "11", "51", "110", "113", "688")):
        return f"sh{digits}"
    if digits.startswith(("8", "4", "92")):
        return f"bj{digits}"
    raise ValueError(f"无法识别的市场: {code}")

def _request_json(full_url: str, retries: int = 3, retry_delay: float = 0.5) -> dict:
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                full_url,
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/", "Accept": "*/*"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                text = resp.read().decode("utf-8", errors="replace")
            # 响应形如 k={...}; 直接从第一个 { 解析,避免脆弱正则
            start = text.find("{")
            if start < 0:
                raise ValueError(f"腾讯返回无 JSON: {text[:120]}")
            return json.loads(text[start:])
        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(min(retry_delay * (2 ** attempt), 3.0))
    raise RuntimeError(f"腾讯行情请求失败: {last_exc}")

def fetch_daily(code: str, beg: str, end: str, fqt: str = "qfq") -> list[dict]:
    """拉取 [beg, end] 前复权日线。

    返回按日期升序的 list[dict]:
      {trade_date, open, high, low, close, volume, pre_close, pct_chg}
    pct_chg 为百分比(收盘相对前收),首日为 None。
    """
    symbol = to_tencent_code(code)
    param = f"{symbol},day,{beg},{end},{MAX_DAYS},{fqt}"
    full = f"{URL}?_var=k&param={param}&r={random.random()}"
    payload = _request_json(full)
    if payload.get("code") != 0:
        raise RuntimeError(f"腾讯业务错误 code={payload.get('code')} msg={payload.get('msg')}")

    sym_data = (payload.get("data") or {}).get(symbol, {})
    klines = sym_data.get(f"{fqt}day") or sym_data.get("day") or []

    rows: list[dict] = []
    for item in klines:
        if len(item) < 6:
            continue
        try:
            row = {
                "trade_date": str(item[0]),
                "open": float(item[1]),
                "close": float(item[2]),
                "high": float(item[3]),
                "low": float(item[4]),
                "volume": float(item[5]) * 100.0,  # 手 -> 股
            }
        except (TypeError, ValueError):
            continue
        rows.append(row)

    rows.sort(key=lambda r: r["trade_date"])
    # 去重(同日保留最后一条)
    dedup: dict[str, dict] = {r["trade_date"]: r for r in rows}
    rows = [dedup[k] for k in sorted(dedup)]

    # 前收 / 涨跌幅
    prev_close = None
    for r in rows:
        r["pre_close"] = prev_close
        if prev_close and prev_close > 0:
            r["pct_chg"] = round((r["close"] - prev_close) / prev_close * 100.0, 4)
        else:
            r["pct_chg"] = None
        prev_close = r["close"]

    # 只保留区间内(接口的 MAX_DAYS 可能带出更早数据)
    return [r for r in rows if beg <= r["trade_date"] <= end]

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="腾讯公开日线行情抓取")
    parser.add_argument("--code", default="002156")
    parser.add_argument("--beg", default="2026-06-01")
    parser.add_argument("--end", default="2026-07-30")
    args = parser.parse_args()

    data = fetch_daily(args.code, args.beg, args.end)
    print(f"{args.code}: {len(data)} 个交易日 [{args.beg} ~ {args.end}]")
    for r in data[-5:]:
        print(f"  {r['trade_date']} close={r['close']:.2f} pct_chg={r['pct_chg']}")

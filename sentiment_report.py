#!/usr/bin/env python3
"""把结构化情绪结果渲染为适合飞书卡片的 Markdown。"""

from __future__ import annotations

from typing import Any

from sentiment_models import SentimentItem, SentimentReport, panic_band, panic_group


def _short_quote(item: SentimentItem, max_chars: int = 80) -> str:
    if not item.evidence:
        return ""
    quote = " ".join(item.evidence[0].quote.split())
    if len(quote) > max_chars:
        quote = quote[: max_chars - 1] + "…"
    return quote


def _market_text(market: dict[str, Any]) -> str:
    if not market:
        return ""
    parts = []
    pct = market.get("pct_chg")
    ret5 = market.get("return_5d_pct")
    volume_ratio = market.get("volume_ratio_5d")
    if isinstance(pct, (int, float)):
        parts.append(f"当日 {pct:+.2f}%")
    if isinstance(ret5, (int, float)):
        parts.append(f"5日 {ret5:+.2f}%")
    if isinstance(volume_ratio, (int, float)):
        parts.append(f"量比 {volume_ratio:.2f}x")
    if not parts:
        return ""
    date_note = ""
    if market.get("is_target_trade_date") is False and market.get("trade_date"):
        date_note = f"（截至 {market['trade_date']}）"
    return f" · 行情{date_note} " + " / ".join(parts)


def render_markdown(
    report: SentimentReport,
    market_by_code: dict[str, dict[str, Any]] | None = None,
) -> str:
    market_by_code = market_by_code or {}
    lines = [
        f"# 股吧情绪日报 · {report.trade_date.isoformat()} · {report.slot}",
        "",
        f"**大盘背景**：{report.market_context}",
        "",
        "**尺度**：0-30 平静 / 30-50 谨慎 / 50-70 偏恐慌 / "
        "70-85 恐慌 / 85-100 极度恐慌（越高越恐慌）",
        "",
        "---",
    ]

    groups: dict[str, list[SentimentItem]] = {"green": [], "yellow": [], "red": []}
    labels: dict[str, str] = {}
    for item in sorted(report.items, key=lambda value: value.panic_index):
        key, label = panic_group(item.panic_index)
        groups[key].append(item)
        labels[key] = label

    for key in ("green", "yellow", "red"):
        items = groups[key]
        if not items:
            continue
        lines.extend(["", f"**{labels[key]}**"])
        for item in items:
            score = f"{item.panic_index:.0f}"
            confidence = f"{item.confidence:.0%}"
            evidence = _short_quote(item)
            evidence_text = f" · 证据：“{evidence}”" if evidence else ""
            market_text = _market_text(market_by_code.get(item.code, {}))
            lines.append(
                f"- {item.name}({item.code}) · **{score} {panic_band(item.panic_index)}**"
                f" · 置信度 {confidence} · 样本 {item.sample_count}{market_text} · {item.summary}"
                f"{evidence_text}"
            )

    average = sum(item.panic_index for item in report.items) / len(report.items)
    most_panic = max(report.items, key=lambda value: value.panic_index)
    calmest = min(report.items, key=lambda value: value.panic_index)
    if len(report.items) == 1:
        conclusion = (
            f"{most_panic.name}当前恐慌指数 {most_panic.panic_index:.0f}"
            f"（{panic_band(most_panic.panic_index)}）。"
        )
    else:
        conclusion = (
            f"标的等权平均恐慌指数 {average:.0f}（{panic_band(average)}）；"
            f"最恐慌为 {most_panic.name}({most_panic.panic_index:.0f})，"
            f"最平静为 {calmest.name}({calmest.panic_index:.0f})。"
        )
    lines.extend([
        "",
        "---",
        "",
        f"**一句话结论**：{conclusion}",
        "",
        "> 判读方式：LLM 逐帖按固定绝对尺评分；测量当日情绪，不预测涨跌。",
    ])
    if report.model:
        lines.append(f"> 模型：{report.model}；生成时间：{report.generated_at}")
    if report.source_manifest:
        lines.append(f"> 数据清单：`{report.source_manifest}`")
    return "\n".join(lines) + "\n"

#!/usr/bin/env python3
"""结构化情绪判读的数据模型、校验与 JSON 读写。"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SECURITY_TYPES = ("stock", "etf", "index")


class SentimentValidationError(ValueError):
    """结构化判读结果不符合数据契约。"""


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise SentimentValidationError(f"{field} 不能为空")
    return text


def panic_band(value: float) -> str:
    if value < 30:
        return "平静"
    if value < 50:
        return "谨慎"
    if value < 70:
        return "偏恐慌"
    if value < 85:
        return "恐慌"
    return "极度恐慌"


def panic_group(value: float) -> tuple[str, str]:
    if value < 50:
        return "green", "🟢 亢奋/乐观"
    if value < 70:
        return "yellow", "🟡 纠结/偏谨慎"
    return "red", "🔴 失落/恐慌"


@dataclass(frozen=True)
class Evidence:
    post_id: str
    quote: str
    source: str = ""

    @classmethod
    def from_dict(cls, raw: Any, field: str) -> "Evidence":
        if not isinstance(raw, dict):
            raise SentimentValidationError(f"{field} 必须是 object")
        return cls(
            post_id=_required_text(raw.get("post_id"), f"{field}.post_id"),
            quote=_required_text(raw.get("quote"), f"{field}.quote"),
            source=str(raw.get("source") or "").strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"post_id": self.post_id, "quote": self.quote, "source": self.source}


@dataclass(frozen=True)
class SentimentItem:
    code: str
    name: str
    security_type: str
    panic_index: float
    confidence: float
    sample_count: int
    mean_sentiment: float
    summary: str
    evidence: tuple[Evidence, ...]

    @classmethod
    def from_dict(cls, raw: Any, field: str) -> "SentimentItem":
        if not isinstance(raw, dict):
            raise SentimentValidationError(f"{field} 必须是 object")
        code = _required_text(raw.get("code"), f"{field}.code")
        if not code.isdigit():
            raise SentimentValidationError(f"{field}.code 必须是纯数字代码")
        security_type = str(raw.get("security_type") or "stock").strip()
        if security_type not in SECURITY_TYPES:
            raise SentimentValidationError(
                f"{field}.security_type 只能是 {'/'.join(SECURITY_TYPES)}"
            )
        try:
            panic_raw = raw["panic_index"]
            confidence_raw = raw["confidence"]
            sample_raw = raw["sample_count"]
            if isinstance(panic_raw, bool) or isinstance(confidence_raw, bool):
                raise TypeError("boolean is not a score")
            if isinstance(sample_raw, bool) or not isinstance(sample_raw, int):
                raise TypeError("sample_count is not an integer")
            panic = float(panic_raw)
            confidence = float(confidence_raw)
            sample_count = sample_raw
        except (KeyError, TypeError, ValueError) as exc:
            raise SentimentValidationError(
                f"{field} 的 panic_index/confidence/sample_count 类型无效"
            ) from exc
        if not 0 <= panic <= 100:
            raise SentimentValidationError(f"{field}.panic_index 必须在 0~100")
        if not 0 <= confidence <= 1:
            raise SentimentValidationError(f"{field}.confidence 必须在 0~1")
        if sample_count < 0:
            raise SentimentValidationError(f"{field}.sample_count 不能为负数")

        derived_mean = (50.0 - panic) / 50.0
        if raw.get("mean_sentiment") is None:
            mean_sentiment = derived_mean
        else:
            try:
                mean_sentiment = float(raw["mean_sentiment"])
            except (TypeError, ValueError) as exc:
                raise SentimentValidationError(f"{field}.mean_sentiment 类型无效") from exc
            if not -1 <= mean_sentiment <= 1:
                raise SentimentValidationError(f"{field}.mean_sentiment 必须在 -1~1")
            if abs(mean_sentiment - derived_mean) > 0.03:
                raise SentimentValidationError(
                    f"{field}.mean_sentiment 与 panic_index 的自然映射不一致"
                )

        evidence_raw = raw.get("evidence", [])
        if not isinstance(evidence_raw, list):
            raise SentimentValidationError(f"{field}.evidence 必须是 array")
        evidence = tuple(
            Evidence.from_dict(value, f"{field}.evidence[{index}]")
            for index, value in enumerate(evidence_raw)
        )
        return cls(
            code=code,
            name=_required_text(raw.get("name"), f"{field}.name"),
            security_type=security_type,
            panic_index=panic,
            confidence=confidence,
            sample_count=sample_count,
            mean_sentiment=mean_sentiment,
            summary=_required_text(raw.get("summary"), f"{field}.summary"),
            evidence=evidence,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "security_type": self.security_type,
            "panic_index": self.panic_index,
            "confidence": self.confidence,
            "sample_count": self.sample_count,
            "mean_sentiment": self.mean_sentiment,
            "summary": self.summary,
            "evidence": [value.to_dict() for value in self.evidence],
        }


@dataclass(frozen=True)
class SentimentReport:
    trade_date: dt.date
    slot: str
    generated_at: str
    market_context: str
    model: str
    source_manifest: str
    items: tuple[SentimentItem, ...]
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def from_dict(cls, raw: Any) -> "SentimentReport":
        if not isinstance(raw, dict):
            raise SentimentValidationError("结果根节点必须是 object")
        version = raw.get("schema_version")
        if version != SCHEMA_VERSION:
            raise SentimentValidationError(
                f"不支持 schema_version={version!r}，当前只支持 {SCHEMA_VERSION}"
            )
        run = raw.get("run")
        if not isinstance(run, dict):
            raise SentimentValidationError("run 必须是 object")
        trade_date_text = _required_text(run.get("trade_date"), "run.trade_date")
        try:
            trade_date = dt.date.fromisoformat(trade_date_text)
        except ValueError as exc:
            raise SentimentValidationError("run.trade_date 必须是 YYYY-MM-DD") from exc
        generated_at = _required_text(run.get("generated_at"), "run.generated_at")
        try:
            dt.datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SentimentValidationError("run.generated_at 必须是 ISO-8601 时间") from exc

        items_raw = raw.get("items")
        if not isinstance(items_raw, list) or not items_raw:
            raise SentimentValidationError("items 必须是非空 array")
        items = tuple(
            SentimentItem.from_dict(value, f"items[{index}]")
            for index, value in enumerate(items_raw)
        )
        keys = [(item.code, item.security_type) for item in items]
        if len(keys) != len(set(keys)):
            raise SentimentValidationError("items 存在重复标的")
        return cls(
            trade_date=trade_date,
            slot=_required_text(run.get("slot"), "run.slot"),
            generated_at=generated_at,
            market_context=_required_text(run.get("market_context"), "run.market_context"),
            model=str(run.get("model") or "").strip(),
            source_manifest=str(run.get("source_manifest") or "").strip(),
            items=items,
            schema_version=version,
        )

    @classmethod
    def from_path(cls, path: Path) -> "SentimentReport":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SentimentValidationError(f"无法读取结果文件 {path}: {exc}") from exc
        return cls.from_dict(raw)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run": {
                "trade_date": self.trade_date.isoformat(),
                "slot": self.slot,
                "generated_at": self.generated_at,
                "market_context": self.market_context,
                "model": self.model,
                "source_manifest": self.source_manifest,
            },
            "items": [item.to_dict() for item in self.items],
        }

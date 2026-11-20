"""回测前跨源价格交叉校验。

规则:先比价;若 raw close 或 hfq close 的最大相对偏差超过容差,则按覆盖度评分
(instrument 50% + 交易日 30% + 字段完整 20%)选择最全的源整段回测,绝不混源。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Mapping
import pandas as pd
from aquant.data.provider import BarSource, DAILY_COLUMNS
__all__ = ["choose_source", "CrossCheckReport", "SourceScore"]


@dataclass
class SourceScore:
    name: str
    instrument: float
    date: float
    field: float
    note: str = ""

    @property
    def total(self) -> float:
        return 0.5 * self.instrument + 0.3 * self.date + 0.2 * self.field

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "instrument": self.instrument,
                "date": self.date, "field": self.field,
                "total": round(self.total, 6), "note": self.note}


@dataclass
class CrossCheckReport:
    primary: str
    chosen: str
    tolerance: float
    used_fallback: bool
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    scores: dict[str, SourceScore] = field(default_factory=dict)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary": self.primary, "chosen": self.chosen,
            "tolerance": self.tolerance, "used_fallback": self.used_fallback,
            "conflicts": self.conflicts,
            "scores": {k: v.to_dict() for k, v in self.scores.items()},
            "reason": self.reason,
        }


def _frames(sources: Mapping[str, BarSource], oids: list[str]) -> dict[str, dict[str, pd.DataFrame]]:
    out: dict[str, dict[str, pd.DataFrame]] = {}
    for name, src in sources.items():
        out[name] = {}
        for oid in oids:
            try:
                df = src.daily_raw(oid)
                out[name][oid] = df if isinstance(df, pd.DataFrame) else pd.DataFrame()
            except Exception:
                out[name][oid] = pd.DataFrame()
    return out


def _score(name: str, src: BarSource, oids: list[str],
           frames: dict[str, pd.DataFrame],
           calendar: dict[str, set]) -> SourceScore:
    if not oids:
        return SourceScore(name, 0.0, 0.0, 0.0, "无标的")
    available = [o for o in oids if not frames.get(o, pd.DataFrame()).empty]
    instrument = len(available) / len(oids)
    date_ratios, field_ratios = [], []
    for oid in oids:
        df = frames.get(oid, pd.DataFrame())
        if df.empty:
            date_ratios.append(0.0)
            field_ratios.append(0.0)
            continue
        union = calendar.get(oid, set())
        date_ratios.append(len(set(df.index) & union) / len(union) if union else 1.0)
        cols = [c for c in DAILY_COLUMNS if c in df]
        field_ratios.append(float(df[cols].notna().all(axis=1).mean()) if cols else 0.0)
    date = sum(date_ratios) / len(oids)
    field_score = sum(field_ratios) / len(oids)
    return SourceScore(name, instrument, date, field_score)


def _hfq(src: BarSource, oid: str, df: pd.DataFrame) -> pd.Series:
    try:
        factor = src.adjust_factor(oid).reindex(df.index).ffill().fillna(1.0)
    except Exception:
        factor = pd.Series(1.0, index=df.index)
    return df["close"] * factor


def _compare(sources: Mapping[str, BarSource], oids: list[str],
             frames: dict[str, dict[str, pd.DataFrame]],
             tolerance: float) -> list[dict[str, Any]]:
    names = list(sources)
    conflicts: list[dict[str, Any]] = []
    for i, left in enumerate(names):
        for right in names[i + 1:]:
            for oid in oids:
                a, b = frames[left][oid], frames[right][oid]
                if a.empty or b.empty:
                    continue
                common = a.index.intersection(b.index)
                if len(common) < 2:
                    continue
                aa, bb = a.loc[common], b.loc[common]
                raw = float(((aa["close"] - bb["close"]).abs() / bb["close"]).max())
                ha, hb = _hfq(sources[left], oid, aa), _hfq(sources[right], oid, bb)
                hfq = float(((ha - hb).abs() / hb).max())
                if max(raw, hfq) > tolerance:
                    conflicts.append({
                        "left": left, "right": right, "oid": oid,
                        "overlap_days": len(common),
                        "max_raw_diff": round(raw, 6),
                        "max_hfq_diff": round(hfq, 6),
                    })
    return conflicts


def choose_source(sources: Mapping[str, BarSource], oids: list[str],
                  *, primary: str, tolerance: float = 0.005) -> tuple[str, CrossCheckReport]:
    if primary not in sources:
        raise ValueError(f"primary={primary} 不在 sources 中")
    data = _frames(sources, oids)
    calendar: dict[str, set] = {}
    for oid in oids:
        union: set = set()
        for name in sources:
            union |= set(data[name][oid].index)
        calendar[oid] = union
    scores = {
        name: _score(name, src, oids, data[name], calendar)
        for name, src in sources.items()
    }
    conflicts = _compare(sources, oids, data, tolerance)
    if conflicts:
        best = sorted(scores.values(), key=lambda s: (-s.total, s.name != primary))[0]
        report = CrossCheckReport(
            primary=primary, chosen=best.name, tolerance=tolerance,
            used_fallback=best.name != primary, conflicts=conflicts, scores=scores,
            reason=(f"跨源价格超差 {len(conflicts)} 处,按覆盖度评分改用 {best.name} "
                    f"(总分 {best.total:.4f}),整段回测只使用该源,不混源"),
        )
        return best.name, report
    report = CrossCheckReport(
        primary=primary, chosen=primary, tolerance=tolerance,
        used_fallback=False, conflicts=[], scores=scores,
        reason="跨源价格一致或未配置对比源,使用主力源",
    )
    return primary, report

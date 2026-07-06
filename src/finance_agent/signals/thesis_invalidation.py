"""P2c 失效触发器：纯判定函数 + 告警文案（确定性·零 LLM/网络）。

thesis 逐票声明的破坏条件(固定词表)命中已披露事件 → 论点失效 → 止损复核告警。
纯提醒·不改建议/仓位·绝无加仓字样。flag 默认关时调用方不扫（扫描器逐字节不变）。
"""
from __future__ import annotations

from pathlib import Path

_CONFIG_DIR = Path(__file__).parents[3] / "config"
_TI_DEFAULTS = {"enabled": False, "sigma": 1.5, "impact_min": 7}

TRIGGER_TYPES = ("earnings_miss", "news_negative", "price_break",
                 "revenue_decline", "margin_break")


def _load_block(name: str, defaults: dict) -> dict:
    try:
        import yaml
        p = _CONFIG_DIR / "settings.yaml"
        if p.exists():
            with open(p) as f:
                s = yaml.safe_load(f) or {}
            return {**defaults, **(s.get(name, {}) or {})}
    except Exception:
        pass
    return dict(defaults)


def thesis_invalidation_enabled() -> bool:
    return bool(_load_block("thesis_invalidation", _TI_DEFAULTS)["enabled"])


def _ti_cfg() -> dict:
    return _load_block("thesis_invalidation", _TI_DEFAULTS)


def _num(x):
    return x if isinstance(x, (int, float)) and not (isinstance(x, float) and x != x) else None


def check_earnings_miss(sue_score, sigma: float = 1.5) -> bool:
    s = _num(sue_score)
    return s is not None and s <= -sigma


def check_news_negative(impact, sentiment, impact_min: int = 7) -> bool:
    i = _num(impact)
    return i is not None and i >= impact_min and sentiment == "利空"


def check_price_break(close, stop_price) -> bool:
    c, sp = _num(close), _num(stop_price)
    return c is not None and sp is not None and c < sp


def check_revenue_decline(yoy_growths) -> bool:
    vals = [_num(v) for v in (yoy_growths or [])]
    if len(vals) < 2 or any(v is None for v in vals[-2:]):
        return False
    return all(v < 0 for v in vals[-2:])


def check_margin_break(gross_margin, margin_floor) -> bool:
    m, fl = _num(gross_margin), _num(margin_floor)
    return m is not None and fl is not None and m < fl


def match_triggers(pillars, measured: dict, sigma: float = 1.5,
                   impact_min: int = 7) -> list[dict]:
    """pillars=该票声明的触发器列表；measured=trigger_type→实测值。返回命中的 pillar。"""
    hits = []
    for p in pillars or []:
        tt = p.get("trigger_type")
        if tt not in measured:
            continue
        v, thr = measured[tt], p.get("threshold")
        ok = False
        if tt == "earnings_miss":
            ok = check_earnings_miss(v, sigma)
        elif tt == "news_negative":
            ok = check_news_negative(v[0], v[1], impact_min) if isinstance(v, (tuple, list)) else False
        elif tt == "price_break":
            ok = check_price_break(v, thr)
        elif tt == "revenue_decline":
            ok = check_revenue_decline(v)
        elif tt == "margin_break":
            ok = check_margin_break(v, thr)
        if ok:
            hits.append(p)
    return hits


def format_invalidation_alert(ticker: str, pillar: str, trigger_type: str, detail: str) -> str:
    """确定性告警文案·纯提醒·绝无加仓/仓位数值字样。"""
    return (f"⚠️【论点失效】{ticker} 触发你写的失效条件『{pillar}』——{detail}。"
            f"建议止损复核（仅为提醒，不构成操作指令）。")

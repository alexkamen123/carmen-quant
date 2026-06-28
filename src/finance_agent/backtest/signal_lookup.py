# src/finance_agent/backtest/signal_lookup.py
"""
策略信号查找：
  - 从 data/strategy_results/state.json 读取已验证的有效策略
  - 检查当前行情是否触发这些策略的买入信号
  - 返回格式化字符串供 PM prompt 注入
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd

from finance_agent.backtest.discovery import fetch_ohlcv
from finance_agent.backtest.strategies import STRATEGY_REGISTRY

STATE_FILE = Path("data/strategy_results/state.json")

_STRATEGY_LABEL: dict[str, str] = {
    "rsi":        "RSI超卖",
    "ma_align":   "均线多头排列",
    "ma":         "MA金叉",
    "boll":       "布林下轨",
    "mom":        "动量突破",
    "vol_surge":  "量价齐升",
}


def _strategy_label(name: str) -> str:
    for prefix, label in _STRATEGY_LABEL.items():
        if name.startswith(prefix):
            return label
    return name


# 信号性格（cycle9 backtest_signal_profile·24票×3年 alpha 涨/跌市倾向快照，screen_profiles.py 可刷新）：
# 纯展示用、不改任何权重/建议。⚔️进攻型=涨市超额更强(动量)；🛡️抗跌型=跌市超额更强、防守价值(超卖/布林/均线多头)；
# 💤跑输型=两个行情都跑不赢大盘(金叉/放量)。顺序敏感：ma_align 必须先于 ma。
_SIGNAL_ROLE: dict[str, str] = {
    "rsi":        "🛡️抗跌型",
    "ma_align":   "🛡️抗跌型",
    "ma":         "💤跑输型",
    "boll":       "🛡️抗跌型",
    "mom":        "⚔️进攻型",
    "vol_surge":  "💤跑输型",
}


def _strategy_role(name: str) -> str:
    for prefix, role in _SIGNAL_ROLE.items():
        if name.startswith(prefix):
            return role
    return ""


def _t_stat(data: dict) -> float:
    """单调显著性 t = avg_alpha / (std_alpha / sqrt(n))；样本/方差不足返回 0。"""
    n = data.get("n_signals", 0) or 0
    std = data.get("std_alpha", 0) or 0.0
    a = data.get("avg_alpha", 0) or 0.0
    if n < 2 or std <= 0:
        return 0.0
    return a / (std / math.sqrt(n))


def _wilson_low(beat_rate: float, n: int, z: float = 1.96) -> float:
    if n <= 0:
        return 0.0
    p = beat_rate
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return max(0.0, centre - half)


def _is_reliable(data: dict) -> bool:
    """高信赖 = t≥2 且 beat_rate 的 Wilson 下界≥50%（防把高α小样本噪音当强证据）。"""
    return _t_stat(data) >= 2.0 and _wilson_low(data.get("beat_rate", 0) or 0,
                                                 data.get("n_signals", 0) or 0) >= 0.5


def _load_valid_map() -> dict[tuple[str, str], dict]:
    """返回 {(ticker, strategy): result_dict}，只含 passes=True 的记录"""
    if not STATE_FILE.exists():
        return {}
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
        return {
            (r["ticker"], r["strategy"]): r
            for r in state.get("valid_strategies", [])
        }
    except Exception:
        return {}


def _is_triggered(strategy_name: str, ohlcv: pd.DataFrame) -> bool:
    """检查最新一根K线是否触发该策略信号"""
    if strategy_name not in STRATEGY_REGISTRY:
        return False
    fn, params = STRATEGY_REGISTRY[strategy_name]
    try:
        signals = fn(ohlcv, **params)
        return len(signals) > 0 and bool(signals.iloc[-1])
    except Exception:
        return False


def get_active_signals(ticker: str) -> list[dict]:
    """
    返回当前已触发且历史有效的策略列表（按 avg_alpha 降序）。
    每项: {strategy, label, avg_alpha, beat_rate, n_signals}
    """
    valid_map = _load_valid_map()
    if not valid_map:
        return []

    ticker_strategies = {
        strategy: data
        for (t, strategy), data in valid_map.items()
        if t == ticker
    }
    if not ticker_strategies:
        return []

    ohlcv = fetch_ohlcv(ticker)
    if ohlcv is None or len(ohlcv) < 50:
        return []

    active = []
    for strategy_name, data in ticker_strategies.items():
        if _is_triggered(strategy_name, ohlcv):
            active.append({
                "strategy":  strategy_name,
                "label":     _strategy_label(strategy_name),
                "role":      _strategy_role(strategy_name),   # 性格标签（纯展示，不改权重）
                "avg_alpha": data["avg_alpha"],
                "beat_rate": data["beat_rate"],
                "n_signals": data["n_signals"],
                "t_stat":    round(_t_stat(data), 1),
                "reliable":  _is_reliable(data),
                # L2c 自适应权重（flag 关时恒为 1.0，排序与历史完全一致）
                "weight":    _strategy_weight(strategy_name),
            })

    # 高信赖优先，再按 t-stat × 自适应权重降序。
    # weight 默认 1.0（flag 关），开启后也只小幅升降权——绝不放大金额/仓位，只调
    # 「先给 PM 看哪条证据」的次序。不进入任何用户可见文案（避免把内部调权当卖点）。
    active.sort(key=lambda x: (x["reliable"], x["t_stat"] * x["weight"]), reverse=True)
    return active


def _strategy_weight(strategy_name: str) -> float:
    """读 L2c 自适应权重；任何异常/未启用都回退 1.0（绝不拖垮信号查找）。"""
    try:
        from finance_agent.backtest.strategy_weights import get_strategy_weight
        return get_strategy_weight(strategy_name)
    except Exception:
        return 1.0


def format_strategy_evidence(ticker: str) -> str:
    """
    返回 PM prompt 注入字符串。
    无触发信号时返回空字符串。

    示例输出：
      【量化策略信号（历史验证）】
      RSI超卖（rsi_14_30）→ 历史39次，超额+6.84%，跑赢SPY85%
      布林下轨（boll_20_2）→ 历史24次，超额+5.25%，跑赢SPY92%
    """
    active = get_active_signals(ticker)
    if not active:
        return ""

    lines = ["【量化策略信号（历史验证）】"]
    for sig in active[:3]:
        tag = (f"，t={sig['t_stat']}（高信赖）" if sig["reliable"]
               else "，⚠样本不足/不显著仅参考")
        lines.append(
            f"  {sig['label']}（{sig['strategy']}）→ "
            f"历史{sig['n_signals']}次，"
            f"超额{sig['avg_alpha']:+.2f}%，"
            # "跑赢SPY"而非"胜率"（06-12 UX 扫描 P0）：beat_rate 是跑赢基准比例，
            # 不是该股上涨概率——熊市里跌 1% vs SPY 跌 3% 也算"赢"
            f"跑赢SPY{sig['beat_rate']:.0%}{tag}"
        )
    return "\n".join(lines)

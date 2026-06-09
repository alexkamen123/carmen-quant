# src/finance_agent/value/strategy_scorecard.py
"""
策略 edge 记分牌（L2a）：把 302 策略回测（data/strategy_results/state.json，2 年真实历史）
聚合成"哪类信号真有 edge"的带置信记分牌。

这是当前唯一【样本充足】的 alpha 金矿——Carmen 实盘买入战绩仅 1 条，而回测有
302 个有效组合（253 条 n≥30、125 条 t≥2）。L2a 锚这里，不等稀疏实盘。

诚实纪律（复用 value/metrics 同一套闸门，防 p-hacking）：
  - 显著性 = t-stat≥2 且 beat_rate 的 Wilson 95% 下界≥50%
  - 口径独立：state.json 的 avg_alpha 是回测自有窗口，绝不与 recommendations 的
    7日 benchmark alpha 同表相加（见 value/report 已知局限）。
"""
import json
import math
from pathlib import Path

from finance_agent.value.metrics import wilson_ci, GATE_MIN_N

STATE_FILE = Path("data/strategy_results/state.json")

_FAMILY_LABEL = {
    "rsi": "RSI超卖", "ma_align": "均线多头排列", "ma": "MA金叉",
    "boll": "布林下轨", "mom": "动量突破", "vol_surge": "量价齐升",
}


def _family(strategy: str) -> str:
    for prefix in ("ma_align", "vol_surge", "rsi", "boll", "mom", "ma"):
        if strategy.startswith(prefix):
            return prefix
    return strategy.split("_")[0]


def strategy_t_stat(r: dict) -> float:
    """单调显著性 t = avg_alpha / (std_alpha / sqrt(n))。样本/方差不足返回 0。"""
    n = r.get("n_signals", 0) or 0
    std = r.get("std_alpha", 0) or 0.0
    a = r.get("avg_alpha", 0) or 0.0
    if n < 2 or std <= 0:
        return 0.0
    return a / (std / math.sqrt(n))


def is_reliable(r: dict) -> bool:
    """高信赖 = t≥2 且 beat_rate 的 Wilson 下界≥50%（与 metrics 同口径）。"""
    if strategy_t_stat(r) < 2.0:
        return False
    n = r.get("n_signals", 0) or 0
    correct = round((r.get("beat_rate", 0) or 0) * n)
    lo, _ = wilson_ci(correct, n)
    return lo >= 0.5


def _load_valid() -> list[dict]:
    if not STATE_FILE.exists():
        return []
    try:
        with open(STATE_FILE) as f:
            return json.load(f).get("valid_strategies", [])
    except Exception:
        return []


def compute_strategy_edge() -> dict:
    """
    返回策略族 edge 记分牌 + Top 高信赖个股×策略组合。
    {
      total, reliable_total,
      families: [{family, label, n_combos, total_signals, w_avg_alpha, reliable_combos}],
      top_reliable: [{ticker, strategy, avg_alpha, beat_rate, n_signals, t_stat}],
    }
    """
    valid = _load_valid()
    if not valid:
        return {"total": 0, "reliable_total": 0, "families": [], "top_reliable": []}

    # 按策略族聚合（n 加权 avg_alpha）
    fam: dict[str, dict] = {}
    for r in valid:
        f = _family(r["strategy"])
        n = r.get("n_signals", 0) or 0
        d = fam.setdefault(f, {"n_combos": 0, "total_signals": 0, "alpha_w": 0.0, "reliable": 0})
        d["n_combos"] += 1
        d["total_signals"] += n
        d["alpha_w"] += (r.get("avg_alpha", 0) or 0.0) * n
        if is_reliable(r):
            d["reliable"] += 1

    families = []
    for f, d in fam.items():
        w_avg = (d["alpha_w"] / d["total_signals"]) if d["total_signals"] else 0.0
        families.append({
            "family": f, "label": _FAMILY_LABEL.get(f, f),
            "n_combos": d["n_combos"], "total_signals": d["total_signals"],
            "w_avg_alpha": round(w_avg, 2), "reliable_combos": d["reliable"],
        })
    families.sort(key=lambda x: x["w_avg_alpha"], reverse=True)

    # Top 高信赖个股×策略（t≥2 且 Wilson≥0.5），按 t-stat 降序
    reliable = [r for r in valid if is_reliable(r)]
    reliable.sort(key=strategy_t_stat, reverse=True)
    top = [{
        "ticker": r["ticker"], "strategy": r["strategy"],
        "avg_alpha": round(r.get("avg_alpha", 0), 2),
        "beat_rate": round((r.get("beat_rate", 0) or 0) * 100),
        "n_signals": r.get("n_signals", 0),
        "t_stat": round(strategy_t_stat(r), 1),
    } for r in reliable[:8]]

    return {
        "total": len(valid),
        "reliable_total": len(reliable),
        "gate": {"min_n": GATE_MIN_N, "t_min": 2.0, "wilson_min": 0.5},
        "families": families,
        "top_reliable": top,
    }


def format_strategy_edge_section(edge: dict) -> str:
    """渲染成飞书 lark_md 文本段（供价值报告用）。"""
    if not edge.get("total"):
        return "**🧪 策略 edge（回测）**：暂无回测数据"
    lines = [
        f"**🧪 策略 edge（302 回测·2年真实历史）** · 高信赖 {edge['reliable_total']}/{edge['total']}",
        "_口径：回测自有窗口 alpha，独立于实盘7日，勿与上方混算_",
        "按策略族（n 加权超额收益）：",
    ]
    for f in edge["families"]:
        lines.append(
            f"• {f['label']}：均α {f['w_avg_alpha']:+.2f}%"
            f"（{f['n_combos']}组合/{f['reliable_combos']}高信赖，共{f['total_signals']}次）"
        )
    if edge["top_reliable"]:
        lines.append("Top 高信赖（t≥2 且胜率下界≥50%）：")
        for t in edge["top_reliable"][:5]:
            lines.append(
                f"• {t['ticker']} {t['strategy']}：α {t['avg_alpha']:+.1f}%、"
                f"跑赢{t['beat_rate']}%、{t['n_signals']}次、t={t['t_stat']}"
            )
    return "\n".join(lines)

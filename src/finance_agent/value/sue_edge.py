"""P2b 扩展：SUE 漂移 edge 影子测量（纯观测·不驱动任何建议）。

从 earnings_surprise_alerts 的**已成熟**事件反事实地测：财报大超预期(beat)后 30 天
做多赚不赚、大爆雷(miss)后 30 天避开/做空对不对——目的是将来用数据『挣加仓资格』，
而不是靠铁律永久禁止激进建议。全程不发任何建议、不花真钱、无选择偏差（每条 beat
都进样本，不用等 PM 真下加仓单）。与『影子选股轨(不花真钱考眼光)』同一套路。

诚实边界（与 cycle12/15 同纪律）：
- 样本 < MIN_SAMPLES → 不下结论（insufficient）。
- 这是 **in-sample 描述性读数**，经样本外/walk-forward 校准前**禁据此加仓**。
- excess = return_30d − benchmark_return_30d（个股 30 日收益超基准多少）。
"""
from __future__ import annotations

from finance_agent.db.tracker import get_sue_alerts
from finance_agent.value.rankic_monitor import spearman_rank_ic

MIN_SAMPLES = 60  # ≥60 outcome 才下结论（与因子校准闸一致）


def _excess(r) -> float | None:
    a, b = r.get("return_30d"), r.get("benchmark_return_30d")
    if a is None or b is None:
        return None
    return a - b


def _side_stats(pts, direction: int, min_samples: int) -> dict:
    """pts=[(sue, excess)]；direction=+1(beat·命中=excess>0) / -1(miss·命中=excess<0)。

    verdict: insufficient(n<min) / edge_present(漂移按假设方向兑现) / no_edge。
    """
    n = len(pts)
    if n == 0:
        return {"n": 0, "hit_rate": None, "mean_excess": None, "rankic": None,
                "verdict": "insufficient"}
    excesses = [e for _, e in pts]
    mean_excess = round(sum(excesses) / n, 3)
    hits = sum(1 for e in excesses if (e > 0 if direction > 0 else e < 0))
    hit_rate = round(hits / n, 3)
    rankic = spearman_rank_ic([s for s, _ in pts], excesses) if n >= 2 else None
    if n < min_samples:
        verdict = "insufficient"
    elif mean_excess != 0 and (mean_excess > 0) == (direction > 0):
        verdict = "edge_present"   # beat 均超额>0 或 miss 均超额<0 → 漂移按方向兑现
    else:
        verdict = "no_edge"
    return {"n": n, "hit_rate": hit_rate, "mean_excess": mean_excess,
            "rankic": rankic, "verdict": verdict}


def sue_edge_reading(db_path=None, asof=None, min_samples: int = MIN_SAMPLES,
                     sigma_threshold: float = 1.5) -> dict:
    """beat/miss 两侧漂移 edge + 整体 RankIC 的纯观测读数（从已成熟事件算）。

    整体 RankIC = SUE 分数 vs 30 日超额 的秩相关（这个因子对收益有没有排序力）。
    """
    rows = get_sue_alerts(matured_only=True, asof=asof, db_path=db_path)
    pts = [(r["sue_score"], _excess(r)) for r in rows
           if r.get("sue_score") is not None and _excess(r) is not None]
    beat = _side_stats([(s, e) for s, e in pts if s >= sigma_threshold], +1, min_samples)
    miss = _side_stats([(s, e) for s, e in pts if s <= -sigma_threshold], -1, min_samples)
    overall_rankic = (spearman_rank_ic([s for s, _ in pts], [e for _, e in pts])
                      if len(pts) >= 2 else None)
    return {"beat": beat, "miss": miss, "overall_rankic": overall_rankic,
            "n_total": len(pts), "min_samples": min_samples,
            "caveat": (f"in-sample 描述性读数；≥{min_samples} 样本且过样本外校准前"
                       f"禁据此加仓（长side PEAD 近年被套利得薄、老数据好看不算数）")}

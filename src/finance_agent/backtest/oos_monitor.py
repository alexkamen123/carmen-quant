# src/finance_agent/backtest/oos_monitor.py
"""cycle15 walk-forward OOS 监视器——给方案A装"持续证明自己还有效"的主裁判。

为什么：方案A(regime_aware_guardrail) 是用截至 2026-06 的数据过 OOS 验证才启用的。
若那段行情特殊、edge 会随时间衰减。这里每月在最新数据上重跑同一套样本外验证
(oos_regime_value)，把 regime-vs-static 增量记成时间序列，衰减就告警让用户决定是否关 flag。

诚实核心(防涨市误报)：护栏只在跌市起作用 → 涨市样本外几乎没跌市可测 → 跌市交易笔数
不足时裁决 insufficient_regime_data(不下结论)，只有跌市数据够 + 连续 K 月 regime 不再
赢 static 才报 decaying。与 cycle5/8「小样本好看=过拟合」同一种自律，反向用。

纯观测：只读历史价格回测、记日志、出裁决，绝不自动改 flag/建议（关 flag 须用户拍板）。
"""
from __future__ import annotations

import json
from pathlib import Path

LOG_PATH = Path("data/oos_regime_log.jsonl")
MIN_DOWN = 30          # 跌市交易笔数闸门：不足则该月不可测（涨市常态）
K_CONSECUTIVE = 3      # 连续几个「可测月」regime 不再赢 static 才报衰减
EDGE_EPS = 0.05        # 与方案A启用门槛对称(oos_regime_value 噪声带=0.05)：edge 跌破即不再是真增量、预警。
                       # K_CONSECUTIVE 连续要求挡单月噪声，所以对称化既诚实又不乱叫。
TRAILING_DAYS = 504    # walk-forward 默认 2 年滚动窗口：旧有利数据老化、对近期行情敏感
FETCH_DAYS = 1100      # 取数周期(~3年)，给 trailing 留富余

# 与 scripts/auto_iter/oos_regime.py 同一宇宙(跨6行业去偏，防单板块过拟合)
_UNIVERSE = [
    "NVDA", "AMD", "AVGO", "TSM", "MU", "QCOM", "KLAC", "LRCX",
    "GOOGL", "MSFT", "AAPL", "META", "PLTR", "CRM", "ADBE", "ORCL",
    "JPM", "GS", "BAC", "MS", "V", "MA", "AXP", "SCHW",
    "UNH", "JNJ", "LLY", "PFE", "ABBV", "MRK", "TMO", "ABT",
    "XOM", "CVX", "CAT", "BA", "GE", "HON", "UNP", "DE",
    "WMT", "COST", "PG", "KO", "HD", "MCD",
]


def _count_down_trades(trades: list[tuple]) -> int:
    """跌市(regime=='down')交易笔数——build_signal_trades 产出的 (tk, fam, regime, alpha)。"""
    return sum(1 for t in trades if t[2] == "down")


def _trim(price_map: dict, bench_df, trailing_days: int | None):
    """滚动窗口：只留每只票最近 trailing_days 行（None=用全部历史）。
    walk-forward 用 trailing 窗口让旧的有利数据老化、对近期行情更敏感。"""
    if not trailing_days:
        return price_map, bench_df
    pm = {tk: (df.tail(trailing_days) if df is not None else df)
          for tk, df in price_map.items()}
    bench = bench_df.tail(trailing_days) if bench_df is not None else bench_df
    return pm, bench


def _edge(regime_a, static_a):
    """regime 相对 static 的 OOS 增量；任一缺失→None（不强行算）。"""
    if regime_a is None or static_a is None:
        return None
    return round(regime_a - static_a, 3)


def oos_snapshot(price_map: dict, bench_df, signal_fns: dict, today: str,
                 horizons: tuple = (7, 30), k: int = 5,
                 trailing_days: int | None = None,
                 build_fn=None, eval_fn=None) -> list[dict]:
    """在(可选滚动窗口的)数据上跑各 horizon 的样本外验证，返回带时间戳的行(每 horizon 一行)。

    build_fn/eval_fn 默认用 cycle12 的 build_signal_trades / oos_regime_value；可注入桩测试。
    """
    from finance_agent.backtest.advice_backtest import (
        build_signal_trades, oos_regime_value)
    build_fn = build_fn or build_signal_trades
    eval_fn = eval_fn or oos_regime_value
    pm, bench = _trim(price_map, bench_df, trailing_days)
    rows = []
    for h in horizons:
        trades = build_fn(pm, bench, signal_fns, horizon=h, regime_ma=50)
        r = eval_fn(trades, list(pm.keys()), k=k)
        rows.append({
            "date": today, "horizon": h,
            "naive_alpha": r["naive_alpha"], "static_alpha": r["static_alpha"],
            "regime_alpha": r["regime_alpha"],
            "regime_edge": _edge(r["regime_alpha"], r["static_alpha"]),
            "n_folds": r["n_folds"], "folds_regime_wins": r["folds_regime_wins"],
            "n_trades": len(trades), "n_down": _count_down_trades(trades),
            "verdict": r["verdict"],
        })
    return rows


def record_oos_snapshot(rows: list[dict], log_path: Path = LOG_PATH) -> None:
    """把快照行 append 到 JSONL（每 horizon 一行）。"""
    if not rows:
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _signal_fns() -> dict:
    from finance_agent.backtest import strategies as S
    return {"rsi": S.rsi_oversold, "boll": S.bollinger_lower,
            "vol_surge": S.volume_surge_up, "ma": S.ma_crossover,
            "ma_align": S.ma_alignment, "mom": S.momentum_breakout}


def run_oos_monitor(today: str | None = None, trailing_days: int | None = TRAILING_DAYS,
                    log_path: Path = LOG_PATH, k: int = 5,
                    horizons: tuple = (7, 30)) -> dict:
    """月度 walk-forward：联网取宇宙+SPY → 跑(滚动窗口)样本外验证 → 记录 → 出衰减裁决。
    每月幂等：同月已记则不重复(防月报重跑灌库)。返回 {rows, decay:{horizon:verdict}}。"""
    from datetime import date as _d
    from finance_agent.backtest.discovery import fetch_ohlcv
    today = today or _d.today().isoformat()
    # 幂等：本月(YYYY-MM)已有记录则只出裁决、不重跑
    if log_path.exists():
        ym = today[:7]
        for ln in log_path.read_text().splitlines():
            if not ln.strip():
                continue
            try:
                rec = json.loads(ln)
            except json.JSONDecodeError:
                continue   # 坏行跳过，不让单行脏数据炸掉整个 monitor
            if rec.get("date", "")[:7] == ym:
                return {"rows": [], "skipped": "already_recorded_this_month",
                        "decay": {h: oos_decay_verdict(log_path, horizon=h) for h in horizons}}
    spy = fetch_ohlcv("SPY", period_days=FETCH_DAYS)
    pm = {t: d for t in _UNIVERSE
          if (d := fetch_ohlcv(t, period_days=FETCH_DAYS)) is not None}
    if spy is None or not pm:
        return {"error": "fetch_failed", "rows": []}
    rows = oos_snapshot(pm, spy, _signal_fns(), today,
                        horizons=horizons, k=k, trailing_days=trailing_days)
    record_oos_snapshot(rows, log_path)
    return {"rows": rows,
            "decay": {h: oos_decay_verdict(log_path, horizon=h) for h in horizons}}


def oos_decay_verdict(log_path: Path = LOG_PATH, horizon: int = 7,
                      k_consecutive: int = K_CONSECUTIVE, min_down: int = MIN_DOWN,
                      eps: float = EDGE_EPS) -> dict:
    """读月度 OOS 记录，判方案A是否衰减。

    只把「跌市样本充足(n_down>=min_down)」的月计为可测月；可测月不足 k_consecutive →
    insufficient_regime_data(诚实：现在没跌市可测、不下结论、不误报)。
    最近 k_consecutive 个可测月若全部 regime_edge<=eps(regime 不再赢 static) → decaying 告警；
    否则 healthy。
    """
    rows = []
    if log_path.exists():
        rows = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
    rows = sorted((r for r in rows if r.get("horizon") == horizon),
                  key=lambda r: r["date"])
    # 可测月：跌市样本够 且 edge 算出来了（n_down>=30 ⇒ edge 必非 None，加 None 守卫兜底一致性）
    testable = [r for r in rows if (r.get("n_down") or 0) >= min_down
                and r.get("regime_edge") is not None]
    if len(testable) < k_consecutive:
        return {"status": "insufficient_regime_data", "horizon": horizon,
                "n_testable": len(testable), "k_needed": k_consecutive,
                "current_edge": testable[-1]["regime_edge"] if testable else None}
    recent = testable[-k_consecutive:]
    if all(r["regime_edge"] <= eps for r in recent):   # testable 保证非 None
        status = "decaying"
    else:
        status = "healthy"
    return {"status": status, "horizon": horizon, "n_testable": len(testable),
            "k_needed": k_consecutive,
            "current_edge": testable[-1]["regime_edge"],
            "recent_edges": [round(r["regime_edge"], 3) for r in recent]}

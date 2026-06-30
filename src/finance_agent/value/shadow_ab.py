# src/finance_agent/value/shadow_ab.py
"""方案A（regime_aware_guardrail）影子 A/B 对照——给护栏装铁证级体温计。

为什么需要：方案A 只改"先给 PM 看哪条信号"的排序、不动金额，上线后看不到反事实
（开护栏 vs 关护栏 哪个更好）。直接看组合涨跌无法归因。这里每天把两套排序的 top
机会篮子都记下来，7 日后回填各自真实超额（vs SPY），攒够分歧样本再对比——造出缺失
的反事实。纯记账、不影响线上任何建议。

口径：
  - on  = 护栏开（按当前行情压反指族后的排序，即线上实况）
  - off = 护栏关（regime=None，原始权重排序，反事实）
  - 只有 regime∈{up,down} 才记（None=取数失败，无对照意义）
  - divergent=两套 top 篮子不同 → 唯一有信息量的样本
  - 回填后比 on_alpha vs off_alpha 篮子均值，攒够样本出裁决
"""
from __future__ import annotations

import json
from datetime import date as _date
from pathlib import Path
from typing import Callable

LOG_PATH = Path("data/shadow_ab.jsonl")
TOP_K = 5          # 每套记 top 几只
HORIZON = 7        # 回填窗口（交易日）
GIVE_UP_DAYS = 40   # 超过此日历日仍取不到数（退市等）→ 止损，停止重试该行
MIN_DIVERGENT = 10  # 分歧样本闸门：不足不出裁决（防小样本自欺）


def _best_score(signals: list[dict], regime: str | None,
                weight_fn: Callable[[str, str | None], float]) -> float:
    """该票最强信号的加权 t 值（排序键）。"""
    return max(s["t_stat"] * weight_fn(s["strategy"], regime) for s in signals)


def compute_ab_picks(opps: list[dict], regime: str | None,
                     weight_fn: Callable[[str, str | None], float],
                     top_k: int = TOP_K) -> dict:
    """同一批机会，护栏开(regime) vs 关(None) 两套排序的 top_k 票篮子 + 是否分歧。

    纯函数：weight_fn 注入（生产=get_strategy_weight），便于测试。
    """
    valid = [o for o in opps if o.get("signals")]   # 空 signals 票跳过（防 max() 崩）

    def rank(reg: str | None) -> list[str]:
        ordered = sorted(valid, key=lambda o: -_best_score(o["signals"], reg, weight_fn))
        return [o["ticker"] for o in ordered[:top_k]]

    on = rank(regime)
    off = rank(None)
    return {"on": on, "off": off, "divergent": on != off}


def record_shadow_ab(opps: list[dict], regime: str | None, today: str,
                     log_path: Path = LOG_PATH,
                     weight_fn: Callable[[str, str | None], float] | None = None,
                     top_k: int = TOP_K) -> None:
    """记一行 A/B 快照到 JSONL（append）。regime=None 不记（无对照意义）。"""
    if regime not in ("up", "down"):
        return
    if weight_fn is None:
        from finance_agent.backtest.strategy_weights import get_strategy_weight
        weight_fn = lambda strat, reg: get_strategy_weight(strat, regime=reg)
    picks = compute_ab_picks(opps, regime, weight_fn, top_k=top_k)
    row = {
        "date": today, "regime": regime,
        "on": picks["on"], "off": picks["off"], "divergent": picks["divergent"],
        "filled": False, "on_alpha": None, "off_alpha": None,
    }
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _basket_alpha(tickers: list[str],
                  alpha_fn: Callable[[str, str], float | None],
                  sig_date: str) -> float | None:
    """篮子超额=各票 7日超额均值，取数失败的票跳过；全失败→None。"""
    vals = [a for tk in tickers if (a := alpha_fn(tk, sig_date)) is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def backfill_shadow_ab(log_path: Path,
                       alpha_fn: Callable[[str, str], float | None],
                       today: str, horizon: int = HORIZON,
                       give_up_days: int = GIVE_UP_DAYS) -> int:
    """给到期且未回填的行填两套篮子真实超额。返回新标 filled 的行数。

    alpha_fn(ticker, signal_date) → 该票自 signal_date 起 horizon 交易日 vs SPY 超额%（None=取数失败/窗口未长够）。
    生产注入 make_live_alpha_fn；测试注入桩。

    口径对齐关键：horizon 是「交易日」，但天数差是「日历日」。跨周末时第 horizon 个交易日
    要等 ~horizon*1.4 个日历日才出现。所以「日历日 >= horizon」只是廉价下限预筛，真正的
    完成判定是「两套篮子超额都算出来了」——没算出来(窗口没长够/取数失败)就不标 filled、
    下轮重试，避免分歧样本被永久丢失。超过 give_up_days 仍取不到(退市等)才止损放弃。
    """
    if not log_path.exists():
        return 0
    # 注意：全量读→改→覆写，非并发安全。record 是 append、backfill 偶发手动/独立调度，
    # 当前撞车概率极低；若将来与 record 同时跑需加文件锁或只重写到期行。
    today_d = _date.fromisoformat(today)
    rows = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
    filled = 0
    for row in rows:
        if row.get("filled"):
            continue
        age = (today_d - _date.fromisoformat(row["date"])).days
        if age < horizon:
            continue   # 廉价下限预筛：连日历日都不够，必然未到期
        on_a = _basket_alpha(row["on"], alpha_fn, row["date"])
        off_a = _basket_alpha(row["off"], alpha_fn, row["date"])
        if on_a is None or off_a is None:
            if age < give_up_days:
                continue   # 窗口没长够/取数失败 → 留待下轮重试，不丢样本
            # 已陈旧仍取不到（退市等）→ 止损，标 filled 停止重试
        row["on_alpha"], row["off_alpha"] = on_a, off_a
        row["filled"] = True
        filled += 1
    if filled:
        log_path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    return filled


def _forward_alpha(close, bench_close, sig_date: str, horizon: int = HORIZON):
    """个股自 sig_date 起 horizon 个交易日 vs 基准超额%；窗口没长够→None。

    纯函数（传 pandas Series），算法对齐 discovery.evaluate_one：
      alpha = (个股7日收益) - (基准同期收益)。基准用 asof 对齐交易日。
    """
    import pandas as pd
    try:
        d = pd.Timestamp(sig_date)
        idx = close.index.searchsorted(d)
        if idx >= len(close) or idx + horizon >= len(close):
            return None
        p0, p7 = float(close.iloc[idx]), float(close.iloc[idx + horizon])
        if p0 <= 0:
            return None
        stock_ret = (p7 - p0) / p0 * 100
        d0, d7 = close.index[idx], close.index[idx + horizon]
        b0, b7 = float(bench_close.asof(d0)), float(bench_close.asof(d7))
        bench_ret = (b7 - b0) / b0 * 100 if b0 > 0 else 0.0
        return round(stock_ret - bench_ret, 4)
    except Exception:
        return None


def make_live_alpha_fn(horizon: int = HORIZON):
    """生产 alpha_fn：联网取 SPY + 各票 OHLCV（缓存），包 _forward_alpha。
    口径与机会卡"跑赢SPY"一致，基准恒 SPY。"""
    from finance_agent.backtest.discovery import fetch_ohlcv
    bench = fetch_ohlcv("SPY")
    bench_close = bench["close"] if bench is not None and "close" in bench else None
    cache: dict[str, object] = {}

    def alpha_fn(ticker: str, sig_date: str):
        if bench_close is None:
            return None
        if ticker not in cache:
            df = fetch_ohlcv(ticker)
            cache[ticker] = df["close"] if df is not None and "close" in df else None
        close = cache[ticker]
        if close is None:
            return None
        return _forward_alpha(close, bench_close, sig_date, horizon)

    return alpha_fn


def sweep(today: str | None = None, log_path: Path = LOG_PATH) -> dict:
    """回填到期样本 + 返回裁决（含 backfilled 行数）。CLI 与每周 value-report 共用。

    日志不存在 → 跳过回填、直接出 insufficient（不联网、不崩）。
    """
    if today is None:
        today = _date.today().isoformat()
    n = 0
    if log_path.exists():
        n = backfill_shadow_ab(log_path, make_live_alpha_fn(), today)
    rep = report_shadow_ab(log_path)
    rep["backfilled"] = n
    return rep


def report_shadow_ab(log_path: Path = LOG_PATH,
                     min_divergent: int = MIN_DIVERGENT, band: float = 0.1) -> dict:
    """汇总分歧样本：on 篮子均值 vs off 篮子均值，出裁决。

    只看 divergent 且两套 alpha 都回填成功的行（非分歧行两套相同、无信息量）。
    verdict: insufficient(样本<闸门) / guardrail_helps(edge>band) /
             guardrail_hurts(edge<-band) / neutral(带内)。band 默认 ±0.1% 防噪声误判。

    诚实局限：on/off 篮子通常高度重叠（护栏只换位少数票），整篮均值会稀释护栏效应、
    抬高达到 band 显著性的门槛——所以需要更长时间/更多分歧样本才能下结论，宁慢勿假。
    """
    rows = []
    if log_path.exists():
        rows = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
    usable = [r for r in rows if r.get("divergent") and r.get("filled")
              and r.get("on_alpha") is not None and r.get("off_alpha") is not None]
    n = len(usable)
    if n == 0:
        return {"verdict": "insufficient", "n": 0,
                "on_mean": None, "off_mean": None, "edge": None}
    on_mean = round(sum(r["on_alpha"] for r in usable) / n, 4)
    off_mean = round(sum(r["off_alpha"] for r in usable) / n, 4)
    edge = round(on_mean - off_mean, 4)
    if n < min_divergent:
        verdict = "insufficient"
    elif edge > band:
        verdict = "guardrail_helps"
    elif edge < -band:
        verdict = "guardrail_hurts"
    else:
        verdict = "neutral"
    return {"verdict": verdict, "n": n,
            "on_mean": on_mean, "off_mean": off_mean, "edge": edge}

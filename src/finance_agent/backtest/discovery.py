# src/finance_agent/backtest/discovery.py
"""
策略发现引擎：
  对每个 (策略, 参数, 股票) 三元组，计算历史信号的 7 日超额收益（alpha），
  找出 avg_alpha > 0 且样本 ≥ 20 的有效策略。

核心指标：
  avg_alpha    — 买入信号后 fwd_days 日相对基准（SPY）的平均超额收益
  beat_rate    — 超额收益 > 0 的信号占比（即跑赢大盘的次数比）
  n_signals    — 有效信号数（过滤掉末尾没有足够前向窗口的）
  passes       — avg_alpha > 0 且 n_signals >= MIN_SAMPLES
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from finance_agent.backtest.strategies import STRATEGY_REGISTRY
from finance_agent.backtest.universe import US_BENCHMARK

FWD_DAYS = 7       # 持有窗口（交易日）
MIN_SAMPLES = 20   # 最少有效信号数
HISTORY_DAYS = 730 # 回测历史长度（~2年）


# ── 数据获取（带简单文件缓存）────────────────────────────────────

def _cache_path(ticker: str) -> Path:
    cache_dir = Path("data/strategy_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{ticker}.csv"


def fetch_ohlcv(ticker: str, period_days: int = HISTORY_DAYS + 30) -> pd.DataFrame | None:
    """
    用 yfinance 拉取日线数据，写入本地 CSV 缓存（当天有效）。
    返回含 open/high/low/close/volume 的 DataFrame，index 为 DatetimeIndex。
    失败返回 None。
    """
    path = _cache_path(ticker)
    today = pd.Timestamp.today().strftime("%Y-%m-%d")

    # 读缓存（当天写入的视为有效）
    if path.exists() and pd.Timestamp(path.stat().st_mtime, unit="s").strftime("%Y-%m-%d") == today:
        try:
            df = pd.read_csv(path, index_col=0, parse_dates=True)
            return df
        except Exception:
            pass

    time.sleep(0.2)  # 避免 yfinance rate limit
    try:
        df = yf.download(ticker, period=f"{period_days}d", interval="1d",
                         progress=False, auto_adjust=True)
        if df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0].lower() for c in df.columns]
        else:
            df.columns = [c.lower() for c in df.columns]
        df = df[["open", "high", "low", "close", "volume"]].dropna()
        df.to_csv(path)
        return df
    except Exception as e:
        print(f"  [fetch] {ticker} 失败: {e}")
        return None


# ── 核心评估函数 ─────────────────────────────────────────────────

@dataclass
class EvalResult:
    ticker: str
    strategy: str
    n_signals: int
    avg_return: float        # 原始 7 日均回报（%）
    avg_alpha: float         # 相对 SPY 的平均超额收益（%）
    beat_rate: float         # 跑赢大盘的比例（0~1）
    std_alpha: float         # alpha 标准差（波动性）
    passes: bool             # avg_alpha > 0 且 n_signals >= MIN_SAMPLES
    params: dict = field(default_factory=dict)


def evaluate_one(
    ticker: str,
    strategy_name: str,
    ohlcv: pd.DataFrame,
    benchmark: pd.DataFrame,
) -> EvalResult | None:
    """
    对单个 (股票, 策略) 组合计算 alpha 指标。
    ohlcv / benchmark 均为日线 DataFrame（index = DatetimeIndex）。
    """
    fn, params = STRATEGY_REGISTRY[strategy_name]

    try:
        signals: pd.Series = fn(ohlcv, **params)
    except Exception as e:
        print(f"  [signal] {ticker}/{strategy_name} 计算失败: {e}")
        return None

    # 只保留回测区间内的信号（末尾留 FWD_DAYS + 2 保证前向窗口）
    valid_end = len(ohlcv) - FWD_DAYS - 1
    signals = signals.iloc[:valid_end]

    signal_idxs = np.where(signals.values)[0]
    if len(signal_idxs) == 0:
        return EvalResult(ticker=ticker, strategy=strategy_name,
                          n_signals=0, avg_return=0.0, avg_alpha=0.0,
                          beat_rate=0.0, std_alpha=0.0, passes=False, params=params)

    close = ohlcv["close"].values
    dates = ohlcv.index

    alphas: list[float] = []
    raw_returns: list[float] = []

    for i in signal_idxs:
        if i + FWD_DAYS >= len(close):
            continue
        p0 = close[i]
        p7 = close[i + FWD_DAYS]
        if p0 <= 0:
            continue
        stock_ret = (p7 - p0) / p0 * 100

        # 找基准同期收益
        signal_date = dates[i]
        exit_date   = dates[i + FWD_DAYS]
        try:
            b0 = float(benchmark["close"].asof(signal_date))
            b7 = float(benchmark["close"].asof(exit_date))
            bench_ret = (b7 - b0) / b0 * 100 if b0 > 0 else 0.0
        except Exception:
            bench_ret = 0.0

        alpha = stock_ret - bench_ret
        alphas.append(alpha)
        raw_returns.append(stock_ret)

    n = len(alphas)
    if n == 0:
        return EvalResult(ticker=ticker, strategy=strategy_name,
                          n_signals=0, avg_return=0.0, avg_alpha=0.0,
                          beat_rate=0.0, std_alpha=0.0, passes=False, params=params)

    avg_alpha  = float(np.mean(alphas))
    beat_rate  = float(np.mean([1 if a > 0 else 0 for a in alphas]))
    std_alpha  = float(np.std(alphas))
    avg_return = float(np.mean(raw_returns))

    passes = (avg_alpha > 0) and (n >= MIN_SAMPLES)

    return EvalResult(
        ticker=ticker, strategy=strategy_name,
        n_signals=n, avg_return=round(avg_return, 3),
        avg_alpha=round(avg_alpha, 3), beat_rate=round(beat_rate, 3),
        std_alpha=round(std_alpha, 3), passes=passes, params=params,
    )


# ── 批量评估（单策略 × 全股票池）────────────────────────────────

def run_strategy_on_universe(
    strategy_name: str,
    universe: list[str],
    verbose: bool = True,
) -> list[EvalResult]:
    """
    对给定策略跑整个股票池，返回所有结果（含失败的）。
    按 avg_alpha 降序排列。
    """
    if verbose:
        print(f"\n[Discovery] 策略: {strategy_name}  股票池: {len(universe)} 只")

    benchmark = fetch_ohlcv(US_BENCHMARK)
    if benchmark is None:
        print(f"  [!] 无法获取基准 {US_BENCHMARK}，跳过此策略")
        return []

    results: list[EvalResult] = []
    for i, ticker in enumerate(universe, 1):
        ohlcv = fetch_ohlcv(ticker)
        if ohlcv is None or len(ohlcv) < 100:
            if verbose:
                print(f"  [{i:2d}/{len(universe)}] {ticker:6s}  数据不足，跳过")
            continue

        result = evaluate_one(ticker, strategy_name, ohlcv, benchmark)
        if result is None:
            continue

        status = "✅" if result.passes else ("⚡" if result.n_signals >= MIN_SAMPLES else "·")
        if verbose:
            print(f"  [{i:2d}/{len(universe)}] {ticker:6s}  "
                  f"n={result.n_signals:3d}  "
                  f"α={result.avg_alpha:+.2f}%  "
                  f"beat={result.beat_rate:.0%}  {status}")
        results.append(result)

    results.sort(key=lambda r: r.avg_alpha, reverse=True)
    passing = [r for r in results if r.passes]
    if verbose:
        print(f"  → 有效策略: {len(passing)}/{len(results)}  "
              f"（avg_alpha>0 且 n≥{MIN_SAMPLES}）")
    return results

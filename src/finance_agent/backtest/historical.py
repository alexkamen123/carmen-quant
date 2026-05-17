"""
历史回测：验证 composite_score 信号的有效性

聚焦三个问题（不用总收益 vs buy-and-hold）：
  1. 买入信号有没有集中在相对低点？（entry_percentile）
  2. 卖出信号有没有在大跌前出现？（sell_before_drop_pct）
  3. 买入信号的后续收益是否高于随机入场基准？（buy_avg_fwd10 vs baseline_fwd10）
"""
import asyncio
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from finance_agent.data.router import DataRouter
from finance_agent.signals.technical import calculate_signals

LOOKBACK = 60          # 信号计算需要的历史窗口（天）
BUY_THRESHOLD = 0.3    # composite_score ≥ 此值 → 买入信号
SELL_THRESHOLD = -0.3  # composite_score ≤ 此值 → 卖出信号
DROP_THRESHOLD = -5.0  # 卖出信号后 N 日跌幅超过此值视为"预警成功"（%）
BOTTOM_ZONE = 0.4      # entry_percentile < 此值视为"低位买入"

_router = DataRouter()


@dataclass
class SignalRecord:
    date: str
    score: float
    signal: Literal["buy", "sell"]
    close_price: float
    fwd_5d: float | None = None    # 5日后收益率(%)
    fwd_10d: float | None = None   # 10日后收益率(%)
    fwd_20d: float | None = None   # 20日后收益率(%)
    entry_percentile: float | None = None  # 0=60日最低点买入, 1=60日最高点买入


@dataclass
class BacktestResult:
    ticker: str
    market: str
    period_days: int
    buy_count: int
    sell_count: int
    buy_avg_fwd10: float | None    # 买入信号后10日平均收益(%)
    sell_avg_fwd10: float | None   # 卖出信号后10日平均收益(%)，负值=好
    baseline_fwd10: float          # 随机入场10日平均收益(%) — 基准
    buy_bottom_pct: float | None   # 买入信号中处于低位(<40%)的比例
    sell_before_drop_pct: float | None  # 卖出信号后20日内出现5%+跌幅的比例
    signals: list[SignalRecord] = field(default_factory=list)
    error: str | None = None


async def backtest_ticker(
    ticker: str,
    market: str,
    days: int = 730,
) -> BacktestResult:
    """对单只股票跑历史回测，返回 BacktestResult"""
    fetch_days = days + LOOKBACK + 25  # 额外留空供滚动窗口和前向观察
    try:
        df = await _router.fetch_ohlcv(ticker, market, days=fetch_days)
    except Exception as e:
        return BacktestResult(
            ticker=ticker, market=market, period_days=days,
            buy_count=0, sell_count=0,
            buy_avg_fwd10=None, sell_avg_fwd10=None, baseline_fwd10=0.0,
            buy_bottom_pct=None, sell_before_drop_pct=None,
            error=str(e),
        )

    if len(df) < LOOKBACK + 25:
        return BacktestResult(
            ticker=ticker, market=market, period_days=days,
            buy_count=0, sell_count=0,
            buy_avg_fwd10=None, sell_avg_fwd10=None, baseline_fwd10=0.0,
            buy_bottom_pct=None, sell_before_drop_pct=None,
            error=f"数据不足（{len(df)} 条，需要 {LOOKBACK + 25}+）",
        )

    close = df["close"].values
    dates = df.index  # DatetimeIndex

    # 回测区间：从 LOOKBACK 开始，末尾留 21 天供前向观察
    end_idx = len(df) - 21
    # 只回测最近 days 天内的信号
    start_idx = max(LOOKBACK, len(df) - days - 21)

    signals: list[SignalRecord] = []

    for i in range(start_idx, end_idx):
        window = df.iloc[i - LOOKBACK + 1: i + 1]
        try:
            sig = calculate_signals(window, ticker)
        except Exception:
            continue

        score = sig.composite_score
        if BUY_THRESHOLD > score > SELL_THRESHOLD:
            continue  # 信号不够强，跳过

        direction: Literal["buy", "sell"] = "buy" if score >= BUY_THRESHOLD else "sell"
        c0 = close[i]

        fwd5  = (close[i + 5]  - c0) / c0 * 100 if i + 5  < len(close) else None
        fwd10 = (close[i + 10] - c0) / c0 * 100 if i + 10 < len(close) else None
        fwd20 = (close[i + 20] - c0) / c0 * 100 if i + 20 < len(close) else None

        # 买入点在60日价格区间内的位置
        window_close = close[max(0, i - 59): i + 1]
        lo, hi = window_close.min(), window_close.max()
        entry_pct = (c0 - lo) / (hi - lo) if hi > lo else 0.5

        signals.append(SignalRecord(
            date=dates[i].strftime("%Y-%m-%d"),
            score=round(score, 3),
            signal=direction,
            close_price=round(float(c0), 2),
            fwd_5d=round(fwd5, 2) if fwd5 is not None else None,
            fwd_10d=round(fwd10, 2) if fwd10 is not None else None,
            fwd_20d=round(fwd20, 2) if fwd20 is not None else None,
            entry_percentile=round(entry_pct, 3),
        ))

    # 基准：所有日期随机入场的10日平均收益
    baseline_returns = [
        (close[i + 10] - close[i]) / close[i] * 100
        for i in range(start_idx, end_idx)
        if i + 10 < len(close)
    ]
    baseline_fwd10 = float(np.mean(baseline_returns)) if baseline_returns else 0.0

    buys  = [s for s in signals if s.signal == "buy"  and s.fwd_10d is not None]
    sells = [s for s in signals if s.signal == "sell" and s.fwd_10d is not None]

    buy_avg_fwd10  = float(np.mean([s.fwd_10d for s in buys]))  if buys  else None
    sell_avg_fwd10 = float(np.mean([s.fwd_10d for s in sells])) if sells else None

    # 买入低位准确率：买入信号处于60日区间低40%的比例
    buys_with_pct = [s for s in buys if s.entry_percentile is not None]
    buy_bottom_pct = (
        sum(1 for s in buys_with_pct if s.entry_percentile < BOTTOM_ZONE) / len(buys_with_pct)
        if buys_with_pct else None
    )

    # 卖出预警准确率：卖出信号后20日内出现 DROP_THRESHOLD 跌幅的比例
    sells_with_fwd20 = [s for s in signals if s.signal == "sell" and s.fwd_20d is not None]
    sell_before_drop_pct = (
        sum(1 for s in sells_with_fwd20 if s.fwd_20d < DROP_THRESHOLD) / len(sells_with_fwd20)
        if sells_with_fwd20 else None
    )

    return BacktestResult(
        ticker=ticker,
        market=market,
        period_days=days,
        buy_count=len(buys),
        sell_count=len(sells),
        buy_avg_fwd10=round(buy_avg_fwd10, 2) if buy_avg_fwd10 is not None else None,
        sell_avg_fwd10=round(sell_avg_fwd10, 2) if sell_avg_fwd10 is not None else None,
        baseline_fwd10=round(baseline_fwd10, 2),
        buy_bottom_pct=round(buy_bottom_pct, 3) if buy_bottom_pct is not None else None,
        sell_before_drop_pct=round(sell_before_drop_pct, 3) if sell_before_drop_pct is not None else None,
        signals=signals,
    )


async def backtest_portfolio(
    holdings: list[dict],
    days: int = 730,
) -> list[BacktestResult]:
    """对全部持仓并发跑回测，跳过纯定投 ETF"""
    tasks = [
        backtest_ticker(h["ticker"], h.get("market", "us"), days)
        for h in holdings
        if not h.get("is_dca")
    ]
    return await asyncio.gather(*tasks)

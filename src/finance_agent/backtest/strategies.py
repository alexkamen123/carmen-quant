# src/finance_agent/backtest/strategies.py
"""
规则型策略定义。每个策略函数接受 OHLCV DataFrame，返回 bool Series（True = 买入信号）。
统一约定：
  - df 列名小写：open/high/low/close/volume
  - 信号发生在当根 K 线收盘后（次日开盘可执行）
  - 返回值 NaN 处理为 False（信号未触发）
"""
import numpy as np
import pandas as pd


# ─── 1. RSI 超卖反转 ────────────────────────────────────────────
def _rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / (loss + 1e-9)
    return 100 - 100 / (1 + rs)


def rsi_oversold(df: pd.DataFrame, period: int = 14, threshold: float = 30) -> pd.Series:
    """RSI(period) < threshold → 超卖，买入信号"""
    rsi = _rsi(df["close"], period)
    return (rsi < threshold).fillna(False)


# ─── 2. 均线金叉（MA Cross）──────────────────────────────────────
def ma_crossover(df: pd.DataFrame, fast: int = 10, slow: int = 30) -> pd.Series:
    """快线从下方穿越慢线（金叉）→ 买入信号"""
    mf = df["close"].rolling(fast).mean()
    ms = df["close"].rolling(slow).mean()
    prev_below = mf.shift(1) <= ms.shift(1)
    curr_above = mf > ms
    return (prev_below & curr_above).fillna(False)


# ─── 3. 布林带下轨触底 ───────────────────────────────────────────
def bollinger_lower(df: pd.DataFrame, period: int = 20, std_mult: float = 2.0) -> pd.Series:
    """收盘价 ≤ 布林下轨 → 均值回归买入信号"""
    ma = df["close"].rolling(period).mean()
    std = df["close"].rolling(period).std()
    lower = ma - std_mult * std
    return (df["close"] <= lower).fillna(False)


# ─── 4. 动量突破 ─────────────────────────────────────────────────
def momentum_breakout(df: pd.DataFrame, lookback: int = 20, pct_threshold: float = 5.0) -> pd.Series:
    """过去 lookback 日涨幅 > pct_threshold% → 强势动量买入信号"""
    ret = df["close"].pct_change(lookback) * 100
    return (ret > pct_threshold).fillna(False)


# ─── 5. 成交量放大 + 价格上涨 ───────────────────────────────────
def volume_surge_up(df: pd.DataFrame, vol_mult: float = 2.0) -> pd.Series:
    """成交量 > N 倍 20 日均量 且 收盘 > 昨收 → 资金流入信号"""
    avg_vol = df["volume"].rolling(20).mean()
    price_up = df["close"] > df["close"].shift(1)
    vol_surge = df["volume"] > avg_vol * vol_mult
    return (price_up & vol_surge).fillna(False)


# ─── 6. 均线多头排列（趋势确认）─────────────────────────────────
def ma_alignment(df: pd.DataFrame, short: int = 5, mid: int = 20, long: int = 60) -> pd.Series:
    """MA5 > MA20 > MA60（多头排列）+ 收盘站上 MA5 → 趋势确认买入"""
    ms = df["close"].rolling(short).mean()
    mm = df["close"].rolling(mid).mean()
    ml = df["close"].rolling(long).mean()
    aligned = (ms > mm) & (mm > ml) & (df["close"] > ms)
    return aligned.fillna(False)


# ─── 策略注册表 ─────────────────────────────────────────────────
# {name: (fn, [param_variants])}
STRATEGY_REGISTRY: dict[str, tuple] = {
    "rsi_14_30":       (rsi_oversold,      {"period": 14, "threshold": 30}),
    "rsi_14_35":       (rsi_oversold,      {"period": 14, "threshold": 35}),
    "rsi_7_30":        (rsi_oversold,      {"period": 7,  "threshold": 30}),
    "rsi_21_25":       (rsi_oversold,      {"period": 21, "threshold": 25}),
    "ma_5_20":         (ma_crossover,      {"fast": 5,  "slow": 20}),
    "ma_10_30":        (ma_crossover,      {"fast": 10, "slow": 30}),
    "ma_5_50":         (ma_crossover,      {"fast": 5,  "slow": 50}),
    "boll_20_2":       (bollinger_lower,   {"period": 20, "std_mult": 2.0}),
    "boll_20_25":      (bollinger_lower,   {"period": 20, "std_mult": 2.5}),
    "mom_20_5":        (momentum_breakout, {"lookback": 20, "pct_threshold": 5.0}),
    "mom_10_3":        (momentum_breakout, {"lookback": 10, "pct_threshold": 3.0}),
    "mom_60_10":       (momentum_breakout, {"lookback": 60, "pct_threshold": 10.0}),
    "vol_surge_15":    (volume_surge_up,   {"vol_mult": 1.5}),
    "vol_surge_20":    (volume_surge_up,   {"vol_mult": 2.0}),
    "ma_align_5_20_60":(ma_alignment,      {"short": 5, "mid": 20, "long": 60}),
}

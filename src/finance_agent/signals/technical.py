import pandas as pd
import pandas_ta as ta
from .models import TechnicalSignals

def calculate_signals(df: pd.DataFrame, ticker: str = "") -> TechnicalSignals:
    """
    输入：OHLCV DataFrame（列名：open/high/low/close/volume，至少30行）
    输出：TechnicalSignals Pydantic 模型
    """
    if len(df) < 30:
        raise ValueError(f"至少需要 30 条数据，当前只有 {len(df)} 条")

    close = df["close"]
    volume = df["volume"]

    # RSI
    rsi_series = ta.rsi(close, length=14)
    rsi = float(rsi_series.dropna().iloc[-1])
    if rsi >= 70:
        rsi_signal = "overbought"
    elif rsi <= 30:
        rsi_signal = "oversold"
    else:
        rsi_signal = "neutral"

    # Moving Averages
    ma5  = float(ta.sma(close, length=5).dropna().iloc[-1])
    ma20 = float(ta.sma(close, length=20).dropna().iloc[-1])
    n60  = min(60, len(df))
    ma60 = float(ta.sma(close, length=n60).dropna().iloc[-1])
    current_close = float(close.iloc[-1])

    sma5_series  = ta.sma(close, length=5).dropna()
    sma20_series = ta.sma(close, length=20).dropna()
    if len(sma5_series) >= 2 and len(sma20_series) >= 2:
        prev_ma5  = float(sma5_series.iloc[-2])
        prev_ma20 = float(sma20_series.iloc[-2])
        if prev_ma5 <= prev_ma20 and ma5 > ma20:
            ma_signal = "golden_cross"
        elif prev_ma5 >= prev_ma20 and ma5 < ma20:
            ma_signal = "death_cross"
        else:
            ma_signal = "neutral"
    else:
        ma_signal = "neutral"

    price_vs_ma20 = (current_close - ma20) / ma20 * 100

    # MACD
    macd_df = ta.macd(close, fast=12, slow=26, signal=9)
    macd_cols = [c for c in macd_df.columns]
    macd_val  = float(macd_df[macd_cols[0]].dropna().iloc[-1])
    macd_sig  = float(macd_df[macd_cols[1]].dropna().iloc[-1])
    macd_hist_series = macd_df[macd_cols[2]].dropna()
    macd_hist = float(macd_hist_series.iloc[-1])
    prev_hist = float(macd_hist_series.iloc[-2]) if len(macd_hist_series) >= 2 else 0.0
    if macd_hist > 0 and macd_hist > prev_hist:
        macd_trend = "bullish"
    elif macd_hist < 0 and macd_hist < prev_hist:
        macd_trend = "bearish"
    else:
        macd_trend = "neutral"

    # Bollinger Bands
    bb = ta.bbands(close, length=20, std=2)
    bb_cols = [c for c in bb.columns]
    # BBU, BBM, BBL 顺序
    upper_cols = [c for c in bb_cols if "U" in c or "upper" in c.lower()]
    lower_cols = [c for c in bb_cols if "L" in c and "B" in c]
    bb_upper = float(bb[upper_cols[0]].dropna().iloc[-1]) if upper_cols else current_close * 1.05
    bb_lower = float(bb[lower_cols[0]].dropna().iloc[-1]) if lower_cols else current_close * 0.95
    bb_range = bb_upper - bb_lower
    bb_position = (current_close - bb_lower) / bb_range if bb_range > 0 else 0.5

    # Volume ratio
    vol_float = volume.astype(float)
    vol_ma20_series = ta.sma(vol_float, length=20).dropna()
    vol_ma20 = float(vol_ma20_series.iloc[-1]) if len(vol_ma20_series) > 0 else 1.0
    volume_ratio = float(vol_float.iloc[-1]) / vol_ma20 if vol_ma20 > 0 else 1.0

    # Change pct
    prev_close = float(close.iloc[-2]) if len(close) > 1 else current_close
    change_pct = (current_close - prev_close) / prev_close * 100

    # Composite score
    score = 0.0
    if rsi_signal == "oversold":
        score += 0.3 * (1 - rsi / 100)
    elif rsi_signal == "overbought":
        score -= 0.3 * (rsi / 100)
    score += 0.25 if ma_signal == "golden_cross" else (-0.25 if ma_signal == "death_cross" else 0)
    score += 0.2 if macd_trend == "bullish" else (-0.2 if macd_trend == "bearish" else 0)
    score += 0.15 * (1 - bb_position * 2)
    if change_pct > 0:
        score += 0.1 * min(volume_ratio - 1, 1)
    score = max(-1.0, min(1.0, score))

    return TechnicalSignals(
        ticker=ticker,
        close=current_close,
        change_pct=change_pct,
        rsi=rsi, rsi_signal=rsi_signal,
        ma5=ma5, ma20=ma20, ma60=ma60,
        ma_signal=ma_signal, price_vs_ma20=price_vs_ma20,
        macd=macd_val, macd_signal=macd_sig, macd_hist=macd_hist,
        macd_trend=macd_trend,
        bb_upper=bb_upper, bb_lower=bb_lower, bb_position=bb_position,
        volume_ratio=volume_ratio,
        composite_score=score,
    )

import pandas as pd
import pandas_ta as ta
from .models import TechnicalSignals

def calculate_signals(df: pd.DataFrame, ticker: str = "") -> TechnicalSignals:
    """
    输入：OHLCV DataFrame（列名：open/high/low/close/volume，至少30行）
    输出：TechnicalSignals Pydantic 模型
    """
    if len(df) < 20:
        raise ValueError(f"至少需要 20 条数据，当前只有 {len(df)} 条")

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

    # MACD（数据不足时安全降级）
    try:
        macd_df = ta.macd(close, fast=12, slow=26, signal=9)
        macd_cols = [c for c in macd_df.columns]
        macd_val_s  = macd_df[macd_cols[0]].dropna()
        macd_sig_s  = macd_df[macd_cols[1]].dropna()
        macd_hist_series = macd_df[macd_cols[2]].dropna()
        macd_val  = float(macd_val_s.iloc[-1])  if len(macd_val_s)  > 0 else 0.0
        macd_sig  = float(macd_sig_s.iloc[-1])  if len(macd_sig_s)  > 0 else 0.0
        macd_hist = float(macd_hist_series.iloc[-1]) if len(macd_hist_series) > 0 else 0.0
        prev_hist = float(macd_hist_series.iloc[-2]) if len(macd_hist_series) >= 2 else 0.0
    except Exception:
        macd_val = macd_sig = macd_hist = prev_hist = 0.0
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

    # ATR(14) — 平均真实波幅，量化价格波动剧烈程度
    try:
        atr_series = ta.atr(df["high"], df["low"], df["close"], length=14)
        atr_val = float(atr_series.dropna().iloc[-1]) if atr_series is not None and len(atr_series.dropna()) > 0 else 0.0
        atr_pct = round(atr_val / current_close * 100, 2) if current_close > 0 else 0.0
    except Exception:
        atr_val = atr_pct = 0.0

    # ADX(14) — 趋势强度，>25 视为强趋势
    try:
        adx_df = ta.adx(df["high"], df["low"], df["close"], length=14)
        adx_col = next((c for c in adx_df.columns if c.startswith("ADX")), None)
        adx_series = adx_df[adx_col].dropna() if adx_col else None
        adx_val = float(adx_series.iloc[-1]) if adx_series is not None and len(adx_series) > 0 else 0.0
    except Exception:
        adx_val = 0.0

    # Change pct
    prev_close = float(close.iloc[-2]) if len(close) > 1 else current_close
    change_pct = (current_close - prev_close) / prev_close * 100

    # 趋势模式判断：ADX > 25 且价格结构为上升趋势（价格 > MA20 > MA60）
    is_uptrend = current_close > ma20 and ma20 > ma60
    trend_mode = adx_val > 25 and is_uptrend

    # Composite score — 双模式
    score = 0.0
    if trend_mode:
        # 趋势模式：严格的"回踩 MA20 支撑买点"，三个条件同时满足才触发
        #   1. 价格确实压到 MA20 下方（-6% ~ -0.5%），而不是随便在附近
        #   2. RSI 处于冷却区（40~62），不是超买延续，也不是恐慌抛售
        #   3. MACD 多头（趋势未断）
        genuine_pullback = -6.0 < price_vs_ma20 < -0.5
        rsi_cooling = 40 < rsi < 62
        score += 0.6 if (genuine_pullback and rsi_cooling and macd_trend == "bullish") else 0.0
        # 成交量放大加分（缩量回踩更健康）
        if change_pct > 0:
            score += 0.1 * min(volume_ratio - 1, 1)
        # 卖出：均线死叉或价格跌破 MA60 才是真正的结构破坏
        score -= 0.5 if ma_signal == "death_cross" else 0.0
        score -= 0.3 if current_close < ma60 else 0.0
    else:
        # 震荡/均值回归模式：原逻辑
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
        atr=round(atr_val, 4),
        atr_pct=atr_pct,
        adx=round(adx_val, 1),
        trend_mode=trend_mode,
        composite_score=score,
    )

import pandas as pd
import numpy as np
import pytest
from finance_agent.signals.technical import calculate_signals
from finance_agent.signals.models import TechnicalSignals

def make_fake_ohlcv(n=60) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    dates = pd.date_range("2025-01-01", periods=n, freq="B")
    close = np.cumsum(rng.standard_normal(n)) + 100
    close = np.abs(close) + 10  # 确保正数
    return pd.DataFrame({
        "open":   close * 0.99,
        "high":   close * 1.01,
        "low":    close * 0.98,
        "close":  close,
        "volume": rng.integers(1_000_000, 10_000_000, n).astype(float),
    }, index=dates)

def test_returns_technical_signals_model():
    df = make_fake_ohlcv(60)
    result = calculate_signals(df)
    assert isinstance(result, TechnicalSignals)

def test_rsi_in_valid_range():
    df = make_fake_ohlcv(60)
    result = calculate_signals(df)
    assert 0 <= result.rsi <= 100

def test_ma_cross_signal_is_valid():
    df = make_fake_ohlcv(60)
    result = calculate_signals(df)
    assert result.ma_signal in ("golden_cross", "death_cross", "neutral")

def test_composite_score_in_range():
    df = make_fake_ohlcv(60)
    result = calculate_signals(df)
    assert -1.0 <= result.composite_score <= 1.0

def test_needs_at_least_30_rows():
    df = make_fake_ohlcv(10)
    with pytest.raises(ValueError, match="至少需要 20 条"):
        calculate_signals(df)

def test_to_prompt_str_contains_ticker():
    df = make_fake_ohlcv(60)
    result = calculate_signals(df, ticker="NVDA")
    prompt = result.to_prompt_str()
    assert "NVDA" in prompt
    assert "RSI" in prompt

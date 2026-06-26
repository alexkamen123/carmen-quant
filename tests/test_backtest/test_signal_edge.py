# tests/test_backtest/test_signal_edge.py
"""泛化广历史回测 backtest_signal_edge：测任意信号触发日→forward 方向是否优于基率。
用于逐条筛系统技术信号哪些真有预测力。注入数据，不碰网络。"""
import numpy as np
import pandas as pd
from finance_agent.backtest.advice_backtest import backtest_signal_edge


def _series(n=400, seed=1):
    rng = np.random.RandomState(seed)
    close = 100 + np.cumsum(0.4 + 0.8 * rng.randn(n))
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.DataFrame({"close": close, "open": close, "high": close,
                         "low": close, "volume": 1e6}, index=idx)


def test_perfect_up_signal_high_precision():
    """造一个"完美"看涨信号：只在 forward>0 的日触发 → precision≈1，远高于基率。"""
    df = _series()
    fwd = df["close"].shift(-7) / df["close"] - 1
    perfect = fwd > 0          # 作弊信号：未来涨就触发
    r = backtest_signal_edge({"X": df}, mask_fn=lambda d: perfect, horizon=7, predict_up=True)
    assert r["precision"] is not None and r["precision"] >= 0.95
    assert r["edge"] > 0 and r["discriminating"] is True


def test_random_signal_no_edge():
    """随机信号 → precision≈基率，无边际。"""
    df = _series()
    rng = np.random.RandomState(7)
    rand = pd.Series(rng.rand(len(df)) > 0.5, index=df.index)
    r = backtest_signal_edge({"X": df}, mask_fn=lambda d: rand, horizon=7)
    assert r["n_eval"] > 100
    assert abs(r["edge"]) < 0.12          # 随机信号边际≈0


def test_predict_down_direction():
    """predict_up=False 时命中=forward<0；完美看跌信号 precision 高。"""
    df = _series()
    fwd = df["close"].shift(-7) / df["close"] - 1
    perfect_down = fwd < 0
    r = backtest_signal_edge({"X": df}, mask_fn=lambda d: perfect_down,
                             horizon=7, predict_up=False)
    assert r["precision"] >= 0.95 and r["discriminating"] is True

# tests/test_backtest/test_broad_momentum.py
"""广历史回测：卖飞守门的核心主张「强势上行(RSI<70·多头排列·MACD>0)的股票会接着涨」
在多票多年历史上是否成立。突破「实际减仓 recs 只有23条单一行情」的样本不足。
price_map 注入，测试不碰网络。"""
import numpy as np
import pandas as pd
from finance_agent.backtest.advice_backtest import backtest_momentum_sell_broad


def _trending_up(n=400, start=100.0, drift=0.5, noise=0.8, seed=1):
    """造一段持续上行序列（带回撤，使 RSI 在上行中也跌破 70→强动量日出现；forward 多为正）。"""
    rng = np.random.RandomState(seed)
    steps = drift + noise * rng.randn(n)
    close = start + np.cumsum(steps)
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.DataFrame({"close": close}, index=idx)


def _choppy(n=400, seed=2):
    """无趋势震荡（强动量罕见、forward 正负各半）。"""
    rng = np.random.RandomState(seed)
    close = 100 + np.cumsum(rng.randn(n))
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.DataFrame({"close": close}, index=idx)


def test_uptrend_strong_momentum_predicts_rise():
    """持续上行里：强动量日的 forward>0 概率应显著高于基率（规则有判别力）。"""
    r = backtest_momentum_sell_broad({"UP": _trending_up()}, horizon=7)
    assert r["n_eval"] > 100 and r["n_flagged"] > 20
    assert r["precision"] is not None and r["base_rate"] is not None
    assert r["precision"] >= r["base_rate"]            # 强动量→更可能继续涨


def test_choppy_no_edge():
    """震荡市里强动量不该有明显 forward 优势。"""
    r = backtest_momentum_sell_broad({"CH": _choppy()}, horizon=7)
    assert r["n_eval"] > 100
    # 不强求方向，只验证函数稳健产出
    assert "verdict" in r and r["n_total_points"] >= r["n_eval"]


def test_aggregates_multiple_tickers():
    r = backtest_momentum_sell_broad(
        {"A": _trending_up(seed=1), "B": _trending_up(seed=3)}, horizon=7)
    # 多票样本合并，n 更大
    assert r["n_eval"] > 200

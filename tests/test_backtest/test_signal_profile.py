# tests/test_backtest/test_signal_profile.py
"""信号体检 backtest_signal_profile：两轴(赚没赚/跑赢没)×分行情(涨市/跌市)×幅度(平均%)+四象限+角色。
比单一 precision 诚实——一个信号涨市靠beta、跌市靠抗跌，单数会糊掉真实角色。注入数据，不碰网络。"""
import pandas as pd
from finance_agent.backtest.advice_backtest import backtest_signal_profile


def _mk(fwds, start=100.0):
    """由 forward 收益序列(horizon=1)反推 close DataFrame：close[i+1]=close[i]*(1+fwd[i])。"""
    closes = [start]
    for f in fwds:
        closes.append(closes[-1] * (1 + f))
    idx = pd.date_range("2024-01-01", periods=len(closes), freq="D")
    return pd.DataFrame({"close": closes}, index=idx)


def _fire_all_but_last(df):
    n = len(df)
    return pd.Series([True] * (n - 1) + [False], index=df.index)


def test_defensive_role_and_quadrants():
    """跌市跑赢(+alpha)但自己仍亏(Q3 少亏) + 涨市跑输(Q2) → 角色=防守(抗跌)。"""
    stock = _mk([-0.01, -0.01, 0.02, 0.02])   # 跌1/跌1/涨2/涨2
    bench = _mk([-0.03, -0.03, 0.03, 0.03])   # 大盘 跌3/跌3/涨3/涨3
    r = backtest_signal_profile({"X": stock}, bench, _fire_all_but_last, horizon=1)
    assert r["n_flagged"] == 4
    assert r["abs_avg"] == 0.5 and r["alpha_avg"] == 0.5      # 平均%
    assert r["down"]["alpha_avg"] == 2.0 and r["down"]["abs_avg"] == -1.0  # 跌市跑赢但绝对仍亏
    assert r["up"]["alpha_avg"] == -1.0                      # 涨市其实跑输(beta)
    assert r["quadrants"]["q3_small_loss"] == 0.5            # 少亏占一半
    assert r["quadrants"]["q2_gain_lag"] == 0.5
    assert r["role"] == "defensive"


def test_offensive_role():
    """涨市跑赢(+alpha,Q1真赚) + 跌市跑输(Q4) → 角色=进攻。"""
    stock = _mk([0.02, 0.02, -0.01, -0.01])
    bench = _mk([0.01, 0.01, -0.005, -0.005])
    r = backtest_signal_profile({"X": stock}, bench, _fire_all_but_last, horizon=1)
    assert r["up"]["alpha_avg"] == 1.0 and r["down"]["alpha_avg"] == -0.5
    assert r["quadrants"]["q1_real_gain"] == 0.5 and r["quadrants"]["q4_real_loss"] == 0.5
    assert r["role"] == "offensive"


def test_quadrants_sum_to_one():
    stock = _mk([0.02, -0.01, 0.02, -0.01])
    bench = _mk([0.01, -0.03, 0.03, 0.005])
    r = backtest_signal_profile({"X": stock}, bench, _fire_all_but_last, horizon=1)
    q = r["quadrants"]
    assert abs(sum(q.values()) - 1.0) < 1e-6


def test_empty_graceful():
    stock = _mk([0.01, 0.01])
    bench = _mk([0.01, 0.01])
    never = lambda df: pd.Series([False] * len(df), index=df.index)
    r = backtest_signal_profile({"X": stock}, bench, never, horizon=1)
    assert r["n_flagged"] == 0 and r["role"] == "weak"


def test_no_benchmark_graceful():
    stock = _mk([0.01, 0.01])
    r = backtest_signal_profile({"X": stock}, None, _fire_all_but_last, horizon=1)
    assert r["n_flagged"] == 0

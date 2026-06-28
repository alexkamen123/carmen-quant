# tests/test_backtest/test_oos_regime.py
"""方案A 样本外验证：按行情切换信号(regime-switching)是否真有增量。
按票 k-fold——训练集学「哪个信号在哪个行情好」，测试集(没见过的票)验。
关键判据：regime 过滤必须赢过 static 过滤，否则价值只是「丢烂信号」(cycle8已做)、与行情无关。
注入 trades，不碰网络。"""
import pandas as pd
from finance_agent.backtest.advice_backtest import oos_regime_value, build_signal_trades


def _synthetic_trades(n_tickers=6):
    """A族:涨市好(+3%)/跌市差(-1%)；B族:涨市差(-1%)/跌市好(+3%)。两族整体都+1%(static留两者)，
    但按行情挑(涨用A、跌用B)能拿+3%——regime 该明显赢过 static。"""
    rows = []
    for i in range(n_tickers):
        tk = f"T{i}"
        rows += [(tk, "A", "up", 0.03), (tk, "A", "down", -0.01),
                 (tk, "B", "up", -0.01), (tk, "B", "down", 0.03)]
    return rows


def test_regime_beats_static_oos():
    trades = _synthetic_trades(6)
    tickers = sorted({t[0] for t in trades})
    r = oos_regime_value(trades, tickers, k=3)
    # 测试集(没见过的票)上：regime ~+3%，static/naive ~+1%
    assert r["regime_alpha"] is not None
    assert r["regime_alpha"] > r["static_alpha"] + 0.5    # regime 明显赢 static（增量真实）
    assert r["static_alpha"] is not None
    assert r["verdict"] == "regime_adds_value"
    assert r["folds_regime_wins"] == r["n_folds"]          # 每折都赢，稳健


def test_static_enough_when_regime_no_extra():
    """A/B 两族都是涨市跌市一样好(+2%)——regime 不该比 static 多拿，判 static_enough。"""
    rows = []
    for i in range(6):
        tk = f"T{i}"
        rows += [(tk, "A", "up", 0.02), (tk, "A", "down", 0.02),
                 (tk, "B", "up", 0.02), (tk, "B", "down", 0.02)]
    r = oos_regime_value(rows, sorted({x[0] for x in rows}), k=3)
    assert abs(r["regime_alpha"] - r["static_alpha"]) < 0.3   # 几乎无差
    assert r["verdict"] in ("static_enough", "no_value")


def test_build_trades_regime_is_causal(monkeypatch):
    """regime 用 SPY vs 其均线(当天可知)，不是未来大盘涨跌——杜绝前视作弊。"""
    idx = pd.date_range("2024-01-01", periods=70, freq="D")
    # SPY 持续上行 → 后段在 MA50 上方 = up-regime
    spy = pd.DataFrame({"close": [100 + i for i in range(70)]}, index=idx)
    stock = pd.DataFrame({"close": [50 + i * 0.5 for i in range(70)]}, index=idx)
    fire = lambda df: pd.Series([True] * len(df), index=df.index)
    trades = build_signal_trades({"X": stock}, spy, {"F": fire}, horizon=7, regime_ma=50)
    assert len(trades) > 0
    # 上行末段必为 up-regime（SPY 在自身 MA50 之上）
    assert any(reg == "up" for (_tk, _fam, reg, _a) in trades)
    assert all(t[1] == "F" for t in trades)

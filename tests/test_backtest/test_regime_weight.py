# tests/test_backtest/test_regime_weight.py
"""方案A 引擎本体：regime-conditional 护栏——读权重时按当前行情压「此行情下反指」的信号族。
OOS 已验证(cycle12) regime-switching 有真增量。flag regime_aware_guardrail 默认关 → 线上零变化。
只影响 PM 材料排序乘数、不动金额/仓位。"""
import pandas as pd
from finance_agent.backtest import strategy_weights as sw
from finance_agent.backtest.advice_backtest import market_regime_from_spy


def _cfg(**over):
    c = dict(sw._DEFAULTS)
    c.update(over)
    return c


def test_regime_flag_off_no_change(monkeypatch):
    """默认关：即便传 regime，vol_surge 权重不被压。"""
    monkeypatch.setattr(sw, "_load_settings", lambda: _cfg(regime_aware_guardrail=False))
    assert sw.get_strategy_weight("vol_surge_2", regime="down") == 1.0   # 无表→1.0，未被额外压


def test_regime_caps_reverse_in_down(monkeypatch):
    """护栏开·跌市：vol_surge(跌市α-0.72) 与 ma(跌市α-0.66) 压到下限。"""
    monkeypatch.setattr(sw, "_load_settings", lambda: _cfg(regime_aware_guardrail=True))
    assert sw.get_strategy_weight("vol_surge_2", regime="down") == sw._DEFAULTS["min_weight"]
    assert sw.get_strategy_weight("ma_10_30", regime="down") == sw._DEFAULTS["min_weight"]


def test_regime_spares_good_in_up(monkeypatch):
    """护栏开·涨市：vol_surge(涨市α+0.16 正) 不压；rsi(两行情都正) 不压。"""
    monkeypatch.setattr(sw, "_load_settings", lambda: _cfg(regime_aware_guardrail=True))
    assert sw.get_strategy_weight("vol_surge_2", regime="up") != sw._DEFAULTS["min_weight"]
    assert sw.get_strategy_weight("rsi_14_30", regime="down") != sw._DEFAULTS["min_weight"]


def test_regime_none_no_cap(monkeypatch):
    """护栏开但没传 regime(取数失败) → 不乱压，回退常规权重。"""
    monkeypatch.setattr(sw, "_load_settings", lambda: _cfg(regime_aware_guardrail=True))
    assert sw.get_strategy_weight("vol_surge_2", regime=None) == 1.0


def test_regime_snapshot_covers_families():
    assert set(sw._REGIME_EDGE_7D) == set(sw._FAMILIES)


def test_market_regime_detector():
    up = pd.Series([100 + i for i in range(60)])      # 持续上行→末值在MA50上方
    dn = pd.Series([100 - i for i in range(60)])
    assert market_regime_from_spy(up, ma=50) == "up"
    assert market_regime_from_spy(dn, ma=50) == "down"
    assert market_regime_from_spy(pd.Series([100, 101]), ma=50) is None   # 数据不足


def test_regime_hysteresis_band_neutral_zone():
    """迟滞带(NIT#1)：收盘贴着均线(±band内)→None，不调权，防抖动。"""
    # 末值略高于 MA50 但在 +1% 带内 → None；明显高于带 → up
    flat = pd.Series([100.0] * 49 + [100.3])   # MA50≈100.006、末值100.3，差<1%
    assert market_regime_from_spy(flat, ma=50, band=0.01) is None
    clear = pd.Series([100.0] * 49 + [103.0])  # 末值高出 ~3% > 1% 带 → up
    assert market_regime_from_spy(clear, ma=50, band=0.01) == "up"

# tests/test_db/test_strategy_edge.py
"""策略 edge 记分牌（L2a）+ signal_lookup 信赖分（order2）测试。"""
import json

from finance_agent.value.strategy_scorecard import (
    strategy_t_stat, is_reliable, _family, compute_strategy_edge,
)
from finance_agent.backtest import signal_lookup


def test_t_stat():
    assert abs(strategy_t_stat({"avg_alpha": 6, "std_alpha": 10, "n_signals": 25}) - 3.0) < 0.01
    assert strategy_t_stat({"avg_alpha": 5, "std_alpha": 0, "n_signals": 10}) == 0.0   # std=0
    assert strategy_t_stat({"avg_alpha": 5, "std_alpha": 10, "n_signals": 1}) == 0.0   # n<2


def test_is_reliable():
    assert is_reliable({"avg_alpha": 6, "std_alpha": 10, "n_signals": 40, "beat_rate": 0.85})
    # 高 α 但小样本(n=5) → Wilson 下界<50% → 不可信（防小样本噪音）
    assert not is_reliable({"avg_alpha": 16, "std_alpha": 10, "n_signals": 5, "beat_rate": 0.80})
    # 低 t（小 α 大方差）→ 不可信
    assert not is_reliable({"avg_alpha": 1, "std_alpha": 30, "n_signals": 40, "beat_rate": 0.85})


def test_family():
    assert _family("rsi_14_30") == "rsi"
    assert _family("ma_align_5_20_60") == "ma_align"   # 不能被 'ma' 抢先
    assert _family("vol_surge_20") == "vol_surge"
    assert _family("ma_5_20") == "ma"


def test_compute_strategy_edge(tmp_path, monkeypatch):
    sj = tmp_path / "state.json"
    sj.write_text(json.dumps({"valid_strategies": [
        {"ticker": "AVGO", "strategy": "rsi_14_30", "n_signals": 40,
         "avg_alpha": 6.0, "std_alpha": 10.0, "beat_rate": 0.85, "passes": True},
        {"ticker": "X", "strategy": "mom_20_5", "n_signals": 300,
         "avg_alpha": 1.0, "std_alpha": 5.0, "beat_rate": 0.55, "passes": True},
        {"ticker": "Y", "strategy": "rsi_7_30", "n_signals": 5,   # 小样本噪音
         "avg_alpha": 20.0, "std_alpha": 8.0, "beat_rate": 0.8, "passes": True},
    ]}))
    monkeypatch.setattr(signal_lookup, "STATE_FILE", sj)  # 不影响（不同模块），仅占位
    monkeypatch.setattr("finance_agent.value.strategy_scorecard.STATE_FILE", sj)
    e = compute_strategy_edge()
    assert e["total"] == 3
    fams = {f["family"] for f in e["families"]}
    assert "rsi" in fams and "mom" in fams
    assert e["reliable_total"] >= 1                       # AVGO 可信
    assert any(t["ticker"] == "AVGO" for t in e["top_reliable"])
    assert not any(t["ticker"] == "Y" for t in e["top_reliable"])  # 小样本被挡在外


def test_signal_lookup_confidence():
    assert signal_lookup._t_stat({"avg_alpha": 6, "std_alpha": 10, "n_signals": 25}) == 3.0
    assert signal_lookup._is_reliable({"avg_alpha": 6, "std_alpha": 10, "n_signals": 40, "beat_rate": 0.85})
    assert not signal_lookup._is_reliable({"avg_alpha": 16, "std_alpha": 10, "n_signals": 5, "beat_rate": 0.8})
    lo = signal_lookup._wilson_low(0.85, 40)
    assert 0.0 <= lo <= 0.85

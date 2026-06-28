# tests/test_backtest/test_signal_role.py
"""信号性格标签（cycle10·方案B 纯透明）：按 cycle9 体检的 alpha 涨/跌市倾向给信号归类，
只在报告里标「⚔️进攻/🛡️抗跌/💤跑输」，不改任何权重/建议。"""
from finance_agent.backtest.signal_lookup import _strategy_role, _SIGNAL_ROLE


def test_role_mapping_by_character():
    assert _strategy_role("mom_20_5") == "⚔️进攻型"           # 动量=涨市赚
    assert _strategy_role("rsi_14_30") == "🛡️抗跌型"          # 超卖=跌市抗跌
    assert _strategy_role("boll_20_2") == "🛡️抗跌型"
    assert _strategy_role("ma_align_5_20_60") == "🛡️抗跌型"   # ma_align 必须先于 ma 匹配
    assert _strategy_role("ma_10_30") == "💤跑输型"           # 金叉=跑输大盘
    assert _strategy_role("vol_surge_2") == "💤跑输型"


def test_role_unknown_returns_empty():
    assert _strategy_role("unknown_x") == ""


def test_role_map_covers_all_label_families():
    """每个有 label 的信号族都该有性格标签，别漏。"""
    from finance_agent.backtest.signal_lookup import _STRATEGY_LABEL
    assert set(_SIGNAL_ROLE) == set(_STRATEGY_LABEL)

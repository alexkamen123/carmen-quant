# tests/test_value/test_takeaway.py
"""一句话串场：把「绿头条(账户赢) + 红明细(买卖亏)」编织成连贯故事，
治用户最初的「自相矛盾/不说人话」痛点。确定性判定、零 LLM，只在特定模式命中时出现。

核心模式：账户跑赢躺平 ∧ 持有判断为正 ∧ 买卖信号为负 = 经典「赚靠拿住、短线买卖在亏」，
点破后给北极星级行为指导「守住好仓、少折腾」。其余组合返回 None，不强行串场。
"""
from finance_agent.value.report import _takeaway


def test_account_won_mainly_by_holding():
    m = {"cumulative": {"excess_pct": 4.7},
         "hold_quality": {"avg_alpha": 6.4},
         "combined_alpha": {"avg": -5.4, "status": "ok"}}
    t = _takeaway(m)
    assert t is not None
    assert "拿住" in t and "少折腾" in t
    assert "6.4" in t and "5.4" in t   # 两个真实数字都点出来


def test_none_when_no_cumulative():
    assert _takeaway({"cumulative": None}) is None
    assert _takeaway({}) is None


def test_none_when_trades_not_losing():
    """账户赢且买卖也赚 → 不是「赚靠拿住」模式，不强行串场。"""
    m = {"cumulative": {"excess_pct": 4.7},
         "hold_quality": {"avg_alpha": 6.4},
         "combined_alpha": {"avg": 3.0, "status": "ok"}}
    assert _takeaway(m) is None


def test_none_when_combined_alpha_unreliable():
    """买卖信号 alpha 不可比(low_coverage/no_sample) → 不下「在亏」断言。"""
    m = {"cumulative": {"excess_pct": 4.7},
         "hold_quality": {"avg_alpha": 6.4},
         "combined_alpha": {"avg": -5.4, "status": "low_coverage"}}
    assert _takeaway(m) is None

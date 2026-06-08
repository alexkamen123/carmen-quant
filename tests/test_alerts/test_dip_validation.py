# tests/test_alerts/test_dip_validation.py
"""暴跌分析自洽性闸门测试：杜绝「加仓价 ≥ 现价」式自相矛盾建议。"""
from finance_agent.alerts.news_monitor import _validate_dip_plan, _extract_price


def test_extract_price_basic():
    assert _extract_price("回踩480企稳后加") == 480.0
    assert _extract_price("550.5 附近") == 550.5
    assert _extract_price("1,234 港元") == 1234.0
    assert _extract_price("") is None
    assert _extract_price("等企稳") is None


def test_gate_rejects_add_price_above_current():
    """加仓价高于现价 → 矛盾，必须剔除具体价并降级。"""
    analysis, warnings = _validate_dip_plan(
        {"action": "加仓", "add_trigger": "550附近加仓"}, price_now=508.0
    )
    assert warnings, "应产生降级警告"
    assert "550" not in analysis["add_trigger"], "矛盾的加仓价应被剔除"


def test_gate_rejects_add_price_equal_current():
    """加仓价等于现价 → 同样视为矛盾（必须严格低于现价）。"""
    analysis, warnings = _validate_dip_plan(
        {"action": "加仓", "add_trigger": "508 加仓"}, price_now=508.0
    )
    assert warnings


def test_gate_keeps_valid_add_price():
    """加仓价低于现价 → 合理，原样保留。"""
    analysis, warnings = _validate_dip_plan(
        {"action": "加仓", "add_trigger": "回踩480企稳后加"}, price_now=508.0
    )
    assert not warnings
    assert "480" in analysis["add_trigger"]


def test_gate_ignores_non_add_actions():
    """非加仓动作（持有/减仓/离场）不含加仓价，不应被改动。"""
    for act in ("持有观望", "减仓", "清仓离场"):
        analysis, warnings = _validate_dip_plan(
            {"action": act, "add_trigger": ""}, price_now=508.0
        )
        assert not warnings


def test_gate_handles_empty():
    analysis, warnings = _validate_dip_plan({}, price_now=508.0)
    assert warnings == []

# tests/test_weekly_card_p3.py
"""周报卡片 P3 渲染测试：P3a 记账断档提醒 / P3b 持仓分层小节。"""
from finance_agent.weekly.report_card import build_weekly_card


def _flat_text(card):
    return " ".join(
        e.get("text", {}).get("content", "")
        for e in card["elements"]
        if e.get("tag") == "div"
    )


def test_action_gap_alert_rendered():
    result = {"action_gap_alert": "📒 已 10 天未记录操作，行为复盘停摆中——有买卖请 `log-action` 补记"}
    card = build_weekly_card(result)
    assert "已 10 天未记录操作" in _flat_text(card)


def test_action_gap_alert_absent_when_none():
    result = {"action_gap_alert": None}
    card = build_weekly_card(result)
    assert "行为复盘停摆" not in _flat_text(card)


def test_holdings_layers_rendered_non_empty_only():
    result = {"holdings_layers": {"hold": ["AAPL"], "add_on_dip": [], "trim": ["NVDA"]}}
    card = build_weekly_card(result)
    text = _flat_text(card)
    assert "AAPL" in text
    assert "NVDA" in text
    assert "回调分批可加" not in text


def test_holdings_layers_absent_when_all_empty():
    result = {"holdings_layers": {"hold": [], "add_on_dip": [], "trim": []}}
    card = build_weekly_card(result)
    assert "持仓分层" not in _flat_text(card)

    result2 = {}
    card2 = build_weekly_card(result2)
    assert "持仓分层" not in _flat_text(card2)


def test_open_guidance_conflict_warning_rendered():
    """P1 接卡片：open 指导项 rationale 含冲突警示句 → 卡片提取渲染警示句，不带整段理由。"""
    result = {"guidance_adherence": {"open": [{
        "action": "减仓", "ticker": "NVDA", "target": "降至15%以下", "due_by": "2026-08-01",
        "rationale": "单票超集中度红线15%；⚠️ 与近日日报方向不同：本条因持仓集中度红线触发，非基本面转空",
    }]}}
    text = _flat_text(build_weekly_card(result))
    assert "⚠️ 与近日日报方向不同" in text
    assert "非基本面转空" in text
    assert "单票超集中度红线15%" not in text  # 整段理由不上卡，只取警示句


def test_open_guidance_no_warning_when_no_conflict():
    result = {"guidance_adherence": {"open": [{
        "action": "减仓", "ticker": "NVDA", "target": "降至15%以下", "due_by": "2026-08-01",
        "rationale": "单票超集中度红线15%",
    }]}}
    assert "与近日日报方向不同" not in _flat_text(build_weekly_card(result))


def test_open_guidance_rationale_missing_ok():
    """老数据无 rationale 键也不崩。"""
    result = {"guidance_adherence": {"open": [{
        "action": "减仓", "ticker": "NVDA", "target": "降至15%以下", "due_by": "2026-08-01",
    }]}}
    assert "NVDA" in _flat_text(build_weekly_card(result))

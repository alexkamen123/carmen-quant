# tests/test_alerts/test_action_trigger.py
"""B1 行动点触达卡：_check_action_trigger + 卡片（零网络）。"""
import pytest

from finance_agent.alerts import news_monitor as nm


def _wire(monkeypatch, plan, price_now):
    from finance_agent.db import tracker
    monkeypatch.setattr(tracker, "get_recent_dip_plan",
                        lambda t, within_days=14, db_path=None: plan)
    monkeypatch.setattr(nm, "_recheck_price", lambda t, m: price_now)


def test_add_trigger_touched(monkeypatch):
    """现价跌到加仓点 → 触发加仓点卡。"""
    _wire(monkeypatch, {"d": "2026-06-10", "action": "加仓",
                        "add_trigger": "回踩 100 企稳后加", "invalidation": ""}, 99.5)
    t = nm._check_action_trigger("NVDA", "us", "2026-06-15", set())
    assert t and t["kind"] == "加仓点" and t["level"] == 100.0
    assert t["dedup_key"] == "action_trig:NVDA:add:2026-06-15"


def test_add_trigger_not_yet(monkeypatch):
    """现价还在加仓点之上 → 不触发。"""
    _wire(monkeypatch, {"d": "2026-06-10", "action": "加仓",
                        "add_trigger": "回踩 100 企稳后加", "invalidation": ""}, 103.0)
    assert nm._check_action_trigger("NVDA", "us", "2026-06-15", set()) is None


def test_invalidation_priority(monkeypatch):
    """认错线优先于加仓点：两者都触达时报认错线（风险更高）。"""
    _wire(monkeypatch, {"d": "2026-06-10", "action": "加仓",
                        "add_trigger": "回踩 100 加", "invalidation": "跌破 90 离场"}, 88.0)
    t = nm._check_action_trigger("NVDA", "us", "2026-06-15", set())
    assert t["kind"] == "认错线" and t["level"] == 90.0


def test_qualitative_plan_no_price_skips(monkeypatch):
    """计划纯定性（无价）→ 不触发，绝不瞎报。"""
    _wire(monkeypatch, {"d": "2026-06-10", "action": "加仓",
                        "add_trigger": "回踩企稳后再加", "invalidation": "下季指引转弱"}, 50.0)
    assert nm._check_action_trigger("NVDA", "us", "2026-06-15", set()) is None


def test_dedup_same_day(monkeypatch):
    """同日同类型已推过 → 不重复。"""
    _wire(monkeypatch, {"d": "2026-06-10", "action": "加仓",
                        "add_trigger": "回踩 100 加", "invalidation": ""}, 99.0)
    alerted = {"action_trig:NVDA:add:2026-06-15"}
    assert nm._check_action_trigger("NVDA", "us", "2026-06-15", alerted) is None


def test_no_plan(monkeypatch):
    _wire(monkeypatch, None, 99.0)
    assert nm._check_action_trigger("NVDA", "us", "2026-06-15", set()) is None


@pytest.mark.asyncio
async def test_card_render(monkeypatch):
    sent = []
    monkeypatch.setattr(nm, "send_feishu_card", lambda card, **kw: _noop(sent, card))
    await nm._send_action_trigger_alert(
        "00700", "hk", kind="加仓点", price_now=448.0, level=450.0,
        plan_text="回踩 450 企稳后小加", plan_date="2026-06-10", dedup_key="x")
    blob = str(sent[0])
    assert "加仓点到了" in blob and "HK$448" in blob and "HK$450" in blob
    assert "回踩 450 企稳后小加" in blob and "提醒非指令" in blob


async def _noop(sink, card):
    sink.append(card)

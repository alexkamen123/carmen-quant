# tests/test_db/test_behavior_hint.py
"""order9：行为时机提示（get_behavior_hint_stats / format_behavior_hint + 触点集成）。"""
import pytest

from finance_agent.db import tracker


def _seed_action(db, action, ret, ticker="NVDA"):
    with tracker._conn(db) as con:
        con.execute(
            "INSERT INTO user_actions(date, ticker, action, actual_return) "
            "VALUES('2026-06-01', ?, ?, ?)",
            (ticker, action, ret),
        )


def test_no_actions_returns_none(tmp_path):
    db = tmp_path / "t.db"
    tracker.init_db(db)
    assert tracker.get_behavior_hint_stats(db_path=db) is None


def test_below_min_n_buy_silent(tmp_path):
    """n_buy=3 < min_n_buy=5 → None（小样本完全静默，不给噪音提示）。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    for r in (-2.0, -3.0, 1.0):
        _seed_action(db, "BUY", r)
    assert tracker.get_behavior_hint_stats(db_path=db) is None


def test_stats_computed(tmp_path):
    """n_buy=6 avg=-4.8；SELL 卖飞计数按 sell_regret_pct=5.0 过线才算。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    for r in (-6.0, -5.0, -4.0, -5.8, -4.0, -4.0):
        _seed_action(db, "BUY", r)
    _seed_action(db, "SELL", 19.4)   # 卖飞
    _seed_action(db, "TRIM", 6.0)    # 卖飞
    _seed_action(db, "SELL", 2.0)    # 未过 5% 线，不计
    _seed_action(db, "SELL", None)   # 未回填，不计
    s = tracker.get_behavior_hint_stats(db_path=db)
    assert s["n_buy"] == 6 and s["avg_buy"] == -4.8
    assert s["n_sell_regret"] == 2
    assert s["small_sample"] is True


def test_format_feishu_small_sample_with_regret():
    s = {"n_buy": 11, "avg_buy": -6.1, "n_sell_regret": 3,
         "sell_regret_pct": 5.0, "small_sample": True}
    out = tracker.format_behavior_hint(s)
    assert "11 次" in out and "-6.1%" in out
    assert "样本少，仅参考" in out
    assert "3 次卖出后涨了 5%+" in out


def test_format_feishu_large_sample_positive():
    """n=20 avg=+2.3 → 含 +2.3%，无小样本标注；卖飞 <2 次不提（避免单例放大）。"""
    s = {"n_buy": 20, "avg_buy": 2.3, "n_sell_regret": 1,
         "sell_regret_pct": 5.0, "small_sample": False}
    out = tracker.format_behavior_hint(s)
    assert "+2.3%" in out and "样本少" not in out and "卖出后涨了" not in out


def test_format_cli_style():
    s = {"n_buy": 11, "avg_buy": -6.1, "n_sell_regret": 3,
         "sell_regret_pct": 5.0, "small_sample": True}
    out = tracker.format_behavior_hint(s, style="cli")
    assert "确认这笔在计划内" in out and "-6.1%" in out


def test_disabled_returns_none(tmp_path, monkeypatch):
    """behavior_hint.enabled=false → None（一键关，两个触点同时静默）。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    for r in (-2.0,) * 6:
        _seed_action(db, "BUY", r)
    monkeypatch.setattr(
        tracker, "_load_settings_block",
        lambda key, defaults: {**defaults, "enabled": False} if key == "behavior_hint"
        else dict(defaults),
    )
    assert tracker.get_behavior_hint_stats(db_path=db) is None


@pytest.mark.asyncio
async def test_dip_card_integration(monkeypatch):
    """触点 A：action=加仓 → 卡片含'行为提示' note；action=持有观望 → 无。"""
    from finance_agent.alerts import news_monitor as nm

    sent = []
    monkeypatch.setattr(nm, "send_feishu_card",
                        lambda card, **kw: _async_noop(sent, card))
    monkeypatch.setattr(
        tracker, "get_behavior_hint_stats",
        lambda db_path=None: {"n_buy": 11, "avg_buy": -6.1, "n_sell_regret": 3,
                              "sell_regret_pct": 5.0, "small_sample": True},
    )

    async def _run(action):
        sent.clear()
        await nm._send_price_drop_alert(
            "NVDA", "us", drop_pct=-5.0, price_now=100.0, price_1h_ago=105.0,
            analysis={"thesis_intact": True, "action": action,
                      "drop_reason": "大盘恐慌", "add_trigger": "回踩100企稳"},
        )
        texts = []
        for el in sent[0]["elements"]:
            if el.get("tag") == "note":
                texts += [x.get("content", "") for x in el.get("elements", [])]
        return texts

    notes = await _run("加仓")
    assert any("行为提示" in t and "11 次" in t for t in notes)
    notes = await _run("持有观望")
    assert not any("行为提示" in t for t in notes)


async def _async_noop(sink, card):
    sink.append(card)

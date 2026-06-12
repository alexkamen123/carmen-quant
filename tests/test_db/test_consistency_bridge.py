# tests/test_db/test_consistency_bridge.py
"""06-13 一致性桥 + 跨模块去重 + 宏观分组测试（零网络）。"""
import asyncio

import pytest

from finance_agent.db import tracker


def test_latest_recommendation_excludes_shadow(tmp_path):
    db = tmp_path / "t.db"
    tracker.init_db(db)
    with tracker._conn(db) as con:
        con.execute("INSERT INTO recommendations(date,ticker,recommendation,position_change,"
                    "market,is_watch) VALUES('2026-06-11','00100','持有','维持','hk',0)")
        con.execute("INSERT INTO recommendations(date,ticker,recommendation,position_change,"
                    "market,is_watch) VALUES('2026-06-12','00100','减仓','减仓','hk',0)")
        con.execute("INSERT INTO recommendations(date,ticker,recommendation,position_change,"
                    "market,is_watch) VALUES('2026-06-13','00100','买入','小加','hk',1)")  # 影子
    lr = tracker.get_latest_recommendation("00100", db_path=db)
    assert lr["date"] == "2026-06-12" and lr["recommendation"] == "减仓"   # 影子行不算


def test_today_dip_conclusion(tmp_path):
    db = tmp_path / "t.db"
    tracker.init_db(db)
    tracker.save_dip_alert("00100", "hk", -5.8, 404.2,
                           {"action": "持有观望", "invalidation": "首份财报远低于预期"},
                           db_path=db)
    out = tracker.get_today_dip_conclusion("00100", db_path=db)
    assert "跌 5.8%" in out and "持有观望" in out and "必须说明改判原因" in out
    assert tracker.get_today_dip_conclusion("NVDA", db_path=db) == ""   # 无记录静默


def test_earnings_recently_alerted(tmp_path, monkeypatch):
    from datetime import date, timedelta
    from finance_agent.alerts import news_monitor as nm
    db = tmp_path / "t.db"
    tracker.init_db(db)
    monkeypatch.setenv("AGENT_DB_PATH", str(db))
    near = (date.today() + timedelta(days=3)).isoformat()
    far = (date.today() + timedelta(days=40)).isoformat()
    with tracker._conn(db) as con:
        con.executescript("CREATE TABLE IF NOT EXISTS news_alerted "
                          "(key TEXT, date TEXT, PRIMARY KEY(key, date))")
        con.execute("INSERT INTO news_alerted VALUES(?, ?)", (f"earnings:NVDA:{near}", "x"))
        con.execute("INSERT INTO news_alerted VALUES(?, ?)", (f"earnings:TSM:{far}", "x"))
    assert nm._earnings_recently_alerted("NVDA") is True
    assert nm._earnings_recently_alerted("TSM") is False    # 40 天外不算临近
    assert nm._earnings_recently_alerted("MU") is False


def test_macro_grouped_picks_highest(monkeypatch, tmp_path):
    """同分钟 5 条 [6,9,8,10,7] → 只推 impact=10 那条（原实现会推 9 丢 10）。"""
    from finance_agent.alerts import news_monitor as nm
    monkeypatch.setattr(nm, "_save_alerted", lambda *a, **k: None)
    news = [{"key": f"k{i}", "published": "09:30", "title": f"t{i}"}
            for i in range(5)]
    impacts = {f"k{i}": v for i, v in enumerate([6, 9, 8, 10, 7])}
    sent = []

    async def classify(n):
        return {"impact": impacts[n["key"]], "sentiment": "利空"}

    async def send(n, impact, result):
        sent.append((n["key"], impact))

    pushed = asyncio.run(nm._push_macro_grouped(
        news, set(), "2026-06-13", 7, classify, send, "macro_slot", "测试"))
    assert pushed == 1 and sent == [("k3", 10)]


def test_macro_grouped_below_threshold_silent(monkeypatch):
    from finance_agent.alerts import news_monitor as nm
    monkeypatch.setattr(nm, "_save_alerted", lambda *a, **k: None)
    news = [{"key": "a", "published": "10:00", "title": "t"}]

    async def classify(n):
        return {"impact": 3}

    async def send(n, impact, result):
        raise AssertionError("低于阈值不该推")

    assert asyncio.run(nm._push_macro_grouped(
        news, set(), "2026-06-13", 7, classify, send, "macro_slot", "测试")) == 0


@pytest.mark.asyncio
async def test_dip_card_shows_last_verdict(monkeypatch, tmp_path):
    """桥的卡片端：dip 卡须带「最近日报裁决」行。"""
    from finance_agent.alerts import news_monitor as nm
    sent = []

    async def fake_send(card, **kw):
        sent.append(card)

    monkeypatch.setattr(nm, "send_feishu_card", fake_send)
    monkeypatch.setattr(tracker, "get_latest_recommendation",
                        lambda t, db_path=None: {"date": "2026-06-12",
                                                 "recommendation": "减仓",
                                                 "position_change": "减仓"})
    await nm._send_price_drop_alert("00100", "hk", drop_pct=-5.0, price_now=400.0,
                                    price_1h_ago=410.0, analysis=None)
    blob = str(sent[0]["elements"])
    assert "最近日报裁决（06-12）：减仓/减仓" in blob
    assert "AI 分析暂不可用" in blob        # 空分析降级说明（UX 批回归顺带验证）

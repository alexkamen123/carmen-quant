import asyncio

from finance_agent.db import tracker
from finance_agent.db import thesis_generator as tg


async def _fake_financials(ticker, market):
    return "暂无财务数据"


def test_generate_parses_structured_triggers(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    tracker.init_db(db)
    fake = (
        "核心论点...\n支撑理由...\n破坏条件...\n当前阶段...\n"
        '```json\n{"pillars":[{"pillar":"毛利","trigger_type":"margin_break",'
        '"threshold":40.0,"status":"intact"}],'
        '"stop_conditions":"毛利跌破40%"}\n```'
    )
    monkeypatch.setattr(tg, "has_claude_cli", lambda: True)
    monkeypatch.setattr(tg, "_fetch_quick_financials", _fake_financials)

    async def _fake_cli(sys, user, timeout=90):
        return fake

    monkeypatch.setattr(tg, "claude_cli_chat", _fake_cli)

    asyncio.run(tg.generate_thesis_for(
        "NVDA", "us", 100.0, 10, "", force=True, db_path=db))

    trig = tracker.load_thesis_triggers("NVDA", db_path=db)
    assert trig is not None
    assert trig["pillars"][0]["trigger_type"] == "margin_break"
    assert trig["stop_conditions"] == "毛利跌破40%"

    text = tracker.load_thesis("NVDA", db_path=db)
    assert "核心论点" in text


def test_generate_parse_failure_degrades(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    tracker.init_db(db)
    monkeypatch.setattr(tg, "has_claude_cli", lambda: True)
    monkeypatch.setattr(tg, "_fetch_quick_financials", _fake_financials)

    async def _fake_cli(sys, user, timeout=90):
        return "只有自由文本没有JSON"

    monkeypatch.setattr(tg, "claude_cli_chat", _fake_cli)

    asyncio.run(tg.generate_thesis_for(
        "AAPL", "us", 100.0, 10, "", force=True, db_path=db))

    assert tracker.load_thesis_triggers("AAPL", db_path=db) is None
    text = tracker.load_thesis("AAPL", db_path=db)
    assert "只有自由文本没有JSON" in text

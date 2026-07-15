import asyncio
import finance_agent.alerts.thesis_invalidation_trigger as it
from finance_agent.db import tracker


def _seed_thesis(db, ticker, pillars):
    tracker.save_thesis(ticker, "us", "正文", pillars=pillars, stop_conditions="", db_path=db)


def test_earnings_scan_hits_declared_trigger(tmp_path, monkeypatch):
    db = tmp_path / "t.db"; tracker.init_db(db)
    _seed_thesis(db, "MU", [{"pillar": "周期", "trigger_type": "earnings_miss", "threshold": None}])
    monkeypatch.setattr(it, "_load_us_holdings", lambda: [{"ticker": "MU", "market": "us"}])
    monkeypatch.setattr(it, "_latest_sue", lambda t, db_path: -2.0)
    monkeypatch.setattr(it, "_fetch_rev_margin", lambda t: None)
    sent = []
    async def _fake_alert(ticker, pillar, tt, detail): sent.append((ticker, tt))
    monkeypatch.setattr(it, "_push_invalidation", _fake_alert)
    monkeypatch.setattr(it, "_today_str", lambda: "2026-07-06")
    asyncio.run(it.scan_earnings_invalidation(db_path=db))
    assert ("MU", "earnings_miss") in sent
    assert len(tracker.get_invalidation_events("MU", db_path=db)) == 1


def test_no_declared_trigger_no_alert(tmp_path, monkeypatch):
    db = tmp_path / "t.db"; tracker.init_db(db)
    _seed_thesis(db, "MU", [{"pillar": "毛利", "trigger_type": "margin_break", "threshold": 40.0}])
    monkeypatch.setattr(it, "_load_us_holdings", lambda: [{"ticker": "MU", "market": "us"}])
    monkeypatch.setattr(it, "_latest_sue", lambda t, db_path: -2.0)   # 爆雷但没声明 earnings_miss
    monkeypatch.setattr(it, "_fetch_rev_margin", lambda t: None)      # 毛利数据缺失→静默
    sent = []
    async def _fake_alert(*a): sent.append(a)
    monkeypatch.setattr(it, "_push_invalidation", _fake_alert)
    monkeypatch.setattr(it, "_today_str", lambda: "2026-07-06")
    asyncio.run(it.scan_earnings_invalidation(db_path=db))
    assert sent == []


def test_single_ticker_failure_isolated(tmp_path, monkeypatch):
    db = tmp_path / "t.db"; tracker.init_db(db)
    _seed_thesis(db, "MU", [{"pillar": "周期", "trigger_type": "earnings_miss", "threshold": None}])
    _seed_thesis(db, "NV", [{"pillar": "周期", "trigger_type": "earnings_miss", "threshold": None}])
    monkeypatch.setattr(it, "_load_us_holdings",
                        lambda: [{"ticker": "MU", "market": "us"}, {"ticker": "NV", "market": "us"}])
    def _sue(t, db_path):
        if t == "MU": raise RuntimeError("boom")
        return -2.0
    monkeypatch.setattr(it, "_latest_sue", _sue)
    monkeypatch.setattr(it, "_fetch_rev_margin", lambda t: None)
    async def _noop(*a): pass
    monkeypatch.setattr(it, "_push_invalidation", _noop)
    monkeypatch.setattr(it, "_today_str", lambda: "2026-07-06")
    asyncio.run(it.scan_earnings_invalidation(db_path=db))
    assert len(tracker.get_invalidation_events("NV", db_path=db)) == 1


def test_price_scan_hits_declared_break(tmp_path, monkeypatch):
    db = tmp_path / "t.db"; tracker.init_db(db)
    _seed_thesis(db, "AAPL", [{"pillar": "止损", "trigger_type": "price_break", "threshold": 150.0}])
    monkeypatch.setattr(it, "_load_us_holdings", lambda: [{"ticker": "AAPL", "market": "us"}])
    monkeypatch.setattr(it, "_latest_close", lambda t: 140.0)   # 跌破 150
    async def _fake_alert(*a): pass
    monkeypatch.setattr(it, "_push_invalidation", _fake_alert)
    monkeypatch.setattr(it, "_today_str", lambda: "2026-07-06")
    asyncio.run(it.scan_price_invalidation(db_path=db))
    assert len(tracker.get_invalidation_events("AAPL", db_path=db)) == 1


def test_news_scan_hits_declared_negative(tmp_path, monkeypatch):
    db = tmp_path / "t.db"; tracker.init_db(db)
    _seed_thesis(db, "TSLA", [{"pillar": "需求", "trigger_type": "news_negative", "threshold": None}])
    async def _fake_alert(*a): pass
    monkeypatch.setattr(it, "_push_invalidation", _fake_alert)
    monkeypatch.setattr(it, "_today_str", lambda: "2026-07-06")
    asyncio.run(it.scan_news_invalidation("TSLA", 8, "利空", db_path=db))
    assert len(tracker.get_invalidation_events("TSLA", db_path=db)) == 1
    # 未声明 news_negative 的票不告
    _seed_thesis(db, "NVDA", [{"pillar": "毛利", "trigger_type": "margin_break", "threshold": 40.0}])
    asyncio.run(it.scan_news_invalidation("NVDA", 9, "利空", db_path=db))
    assert tracker.get_invalidation_events("NVDA", db_path=db) == []

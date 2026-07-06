import json
from finance_agent.db import tracker


def _set_outcome(db, ticker, triggered_at, return_30d, benchmark_return_30d):
    from finance_agent.db.tracker import _conn, _resolve_db
    with _conn(_resolve_db(db)) as con:
        con.execute("UPDATE thesis_invalidation_events SET price_30d=?, return_30d=?, "
                    "benchmark_return_30d=? WHERE ticker=? AND triggered_at=?",
                    (100.0, return_30d, benchmark_return_30d, ticker, triggered_at))


def test_save_invalidation_idempotent(tmp_path):
    db = tmp_path / "t.db"; tracker.init_db(db)
    for _ in range(2):
        tracker.save_invalidation_event(ticker="NVDA", market="us",
            trigger_type="margin_break", pillar="毛利护城河",
            triggered_at="2026-07-06", detail="毛利跌破40%", price_at_event=150.0, db_path=db)
    rows = tracker.get_invalidation_events("NVDA", db_path=db)
    assert len(rows) == 1 and rows[0]["trigger_type"] == "margin_break"
    assert rows[0]["return_30d"] is None


def test_get_invalidation_matured_only(tmp_path):
    db = tmp_path / "t.db"; tracker.init_db(db)
    tracker.save_invalidation_event("MU", "us", "earnings_miss", "周期", "2026-05-01",
                                    "SUE-2σ", 80.0, db_path=db)
    _set_outcome(db, "MU", "2026-05-01", -8.0, -1.0)
    tracker.save_invalidation_event("NV", "us", "price_break", "止损", "2026-06-20",
                                    "跌破150", 140.0, db_path=db)
    matured = tracker.get_invalidation_events(matured_only=True, asof="2026-06-10", db_path=db)
    assert {r["ticker"] for r in matured} == {"MU"}


def test_load_thesis_triggers(tmp_path):
    db = tmp_path / "t.db"; tracker.init_db(db)
    pillars = [{"pillar": "毛利", "trigger_type": "margin_break", "threshold": 40.0}]
    tracker.save_thesis("NVDA", "us", "论点正文", pillars=pillars,
                        stop_conditions="毛利跌破40%", db_path=db)
    trig = tracker.load_thesis_triggers("NVDA", db_path=db)
    assert trig["pillars"][0]["trigger_type"] == "margin_break"
    assert trig["stop_conditions"] == "毛利跌破40%"
    tracker.save_thesis("AAPL", "us", "只有正文", db_path=db)
    assert tracker.load_thesis_triggers("AAPL", db_path=db) is None

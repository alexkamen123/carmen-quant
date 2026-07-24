# tests/test_db/test_periodic_review.py
"""周期性复盘（P3）测试：P3a 记账断档提醒 / P3b 持仓分层聚合。"""
from datetime import datetime, timedelta

from finance_agent.db import tracker
from finance_agent.db.tracker import (
    get_action_gap_alert, layer_holdings, log_user_action, save_recommendations,
    init_db, _conn,
)


# ── P3a 记账断档提醒 ──────────────────────────────────────────

def test_action_gap_alert_recent(tmp_path):
    db = tmp_path / "agent.db"
    log_user_action("NVDA", "BUY", shares=1, price=190, db_path=db)
    assert get_action_gap_alert(db_path=db) is None


def test_action_gap_alert_stale(tmp_path):
    db = tmp_path / "agent.db"
    init_db(db)
    stale_date = (datetime.now().date() - timedelta(days=10)).strftime("%Y-%m-%d")
    with _conn(db) as con:
        con.executescript(tracker._CREATE_ACTIONS_SQL)
        con.execute(
            "INSERT INTO user_actions(date, ticker, action, shares, price, note, rec_date, source) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (stale_date, "NVDA", "BUY", 1, 190, "", stale_date, "manual"),
        )
    alert = get_action_gap_alert(db_path=db)
    assert alert is not None
    assert "10 天" in alert


def test_action_gap_alert_empty_db(tmp_path):
    db = tmp_path / "agent.db"
    alert = get_action_gap_alert(db_path=db)
    assert alert is not None
    assert "尚未记录过任何操作" in alert


# ── P3b 持仓分层 ──────────────────────────────────────────────

def test_layer_holdings_buckets(tmp_path):
    db = tmp_path / "agent.db"
    save_recommendations("2026-07-20", [
        {"ticker": "AAA", "recommendation": "持有"},
        {"ticker": "BBB", "recommendation": "买入"},
        {"ticker": "CCC", "recommendation": "减仓"},
    ], db_path=db)
    layers = layer_holdings(["AAA", "BBB", "CCC"], db_path=db)
    assert layers["hold"] == ["AAA"]
    assert layers["add_on_dip"] == ["BBB"]
    assert layers["trim"] == ["CCC"]


def test_layer_holdings_empty_layer_not_present_when_no_match(tmp_path):
    db = tmp_path / "agent.db"
    save_recommendations("2026-07-20", [
        {"ticker": "AAA", "recommendation": "持有"},
    ], db_path=db)
    layers = layer_holdings(["AAA"], db_path=db)
    assert layers["hold"] == ["AAA"]
    assert layers["add_on_dip"] == []
    assert layers["trim"] == []


def test_layer_holdings_ticker_without_recommendation_skipped(tmp_path):
    db = tmp_path / "agent.db"
    save_recommendations("2026-07-20", [
        {"ticker": "AAA", "recommendation": "持有"},
    ], db_path=db)
    layers = layer_holdings(["AAA", "ZZZ"], db_path=db)
    for tickers in layers.values():
        assert "ZZZ" not in tickers

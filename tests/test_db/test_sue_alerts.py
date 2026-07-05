from finance_agent.db import tracker


def test_save_sue_alert_idempotent(tmp_path):
    db = tmp_path / "t.db"
    tracker.init_db(db)
    for _ in range(2):
        tracker.save_sue_alert(ticker="AAPL", market="us", earnings_date="2026-04-30",
                               sue_score=-1.8, eps_reported=1.5, eps_estimate=1.8,
                               surprise_std=0.15, db_path=db)
    rows = tracker.get_sue_alerts("AAPL", db_path=db)
    assert len(rows) == 1
    assert rows[0]["sue_score"] == -1.8 and rows[0]["return_30d"] is None


def _set_outcome(db, ticker, earnings_date, return_30d, benchmark_return_30d):
    from finance_agent.db.tracker import _conn, _resolve_db
    with _conn(_resolve_db(db)) as con:
        con.execute(
            "UPDATE earnings_surprise_alerts SET price_30d=?, return_30d=?, benchmark_return_30d=? "
            "WHERE ticker=? AND earnings_date=?",
            (100.0, return_30d, benchmark_return_30d, ticker, earnings_date))


def test_get_sue_alerts_matured_only_excludes_immature(tmp_path):
    db = tmp_path / "t.db"
    tracker.init_db(db)
    tracker.save_sue_alert("MU", "us", "2026-05-01", 2.0, 25.1, 20.7, 3.0, db_path=db)
    _set_outcome(db, "MU", "2026-05-01", return_30d=8.0, benchmark_return_30d=2.0)
    tracker.save_sue_alert("NVDA", "us", "2026-06-08", 1.9, 1.87, 1.77, 0.05, db_path=db)
    matured = tracker.get_sue_alerts(matured_only=True, asof="2026-06-10", db_path=db)
    assert {r["ticker"] for r in matured} == {"MU"}

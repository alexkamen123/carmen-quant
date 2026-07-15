import asyncio
import datetime as _dt

import pandas as pd

import finance_agent.alerts.earnings_trigger as et
import finance_agent.db.tracker as tk
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
    # return_30d 已填但距 asof 仅 5 天 → 应被"≥30天闸"单独排除（锁未来函数守则）
    tracker.save_sue_alert("TSLA", "us", "2026-06-05", -1.7, 1.0, 1.3, 0.1, db_path=db)
    _set_outcome(db, "TSLA", "2026-06-05", return_30d=4.0, benchmark_return_30d=1.0)
    matured2 = tracker.get_sue_alerts(matured_only=True, asof="2026-06-10", db_path=db)
    assert "TSLA" not in {r["ticker"] for r in matured2}
    matured = tracker.get_sue_alerts(matured_only=True, asof="2026-06-10", db_path=db)
    assert {r["ticker"] for r in matured} == {"MU"}


def test_backfill_maturity_gate_skips_immature(tmp_path, monkeypatch):
    db = tmp_path / "t.db"; tk.init_db(db)
    monkeypatch.setattr(tk, "_today", lambda: __import__("datetime").datetime(2026, 7, 1))
    tk.save_sue_alert("AAPL", "us", "2026-05-25", -1.8, 1.5, 1.8, 0.15, db_path=db)
    monkeypatch.setattr(tk, "_fetch_paired_window",
                        lambda t, m, d, fwd_td=21: (100.0, 108.0, "2026-05-25", "2026-06-15", 15))
    res = asyncio.run(tk.backfill_sue_outcomes(db_path=db))
    assert res["immature"] == 1 and res["filled"] == 0
    assert tk.get_sue_alerts("AAPL", db_path=db)[0]["return_30d"] is None


def test_backfill_atomic_legs_benchmark_fail_leaves_null(tmp_path, monkeypatch):
    db = tmp_path / "t.db"; tk.init_db(db)
    monkeypatch.setattr(tk, "_today", lambda: __import__("datetime").datetime(2026, 7, 1))
    tk.save_sue_alert("AAPL", "us", "2026-05-01", -1.8, 1.5, 1.8, 0.15, db_path=db)
    monkeypatch.setattr(tk, "_fetch_paired_window",
                        lambda t, m, d, fwd_td=21: (100.0, 110.0, "2026-05-01", "2026-05-30", 25))
    monkeypatch.setattr(tk, "_fetch_benchmark_window", lambda m, s, e: None)
    res = asyncio.run(tk.backfill_sue_outcomes(db_path=db))
    assert res["failed"] == 1 and res["filled"] == 0
    assert tk.get_sue_alerts("AAPL", db_path=db)[0]["return_30d"] is None


def test_backfill_success_writes_both_legs(tmp_path, monkeypatch):
    db = tmp_path / "t.db"; tk.init_db(db)
    monkeypatch.setattr(tk, "_today", lambda: __import__("datetime").datetime(2026, 7, 1))
    tk.save_sue_alert("AAPL", "us", "2026-05-01", -1.8, 1.5, 1.8, 0.15, db_path=db)
    monkeypatch.setattr(tk, "_fetch_paired_window",
                        lambda t, m, d, fwd_td=21: (100.0, 110.0, "2026-05-01", "2026-05-30", 25))
    monkeypatch.setattr(tk, "_fetch_benchmark_window", lambda m, s, e: 3.5)
    res = asyncio.run(tk.backfill_sue_outcomes(db_path=db))
    row = tk.get_sue_alerts("AAPL", db_path=db)[0]
    assert res["filled"] == 1
    assert row["return_30d"] == 10.0 and row["benchmark_return_30d"] == 3.5


def test_record_sue_events_writes_fresh_earnings(tmp_path, monkeypatch):
    db = tmp_path / "t.db"; tk.init_db(db)
    dates = pd.date_range("2023-06-01", periods=12, freq="90D", tz="US/Eastern")
    dates = dates.append(pd.DatetimeIndex([pd.Timestamp("2026-06-30", tz="US/Eastern")]))
    # 历史 12 季 surprise 小幅波动（保证 sigma>0），最新一季大幅高于历史波动区间形成 SUE 信号
    hist_surprises = [0.02, -0.03, 0.01, 0.04, -0.02, 0.03, 0.00, -0.01, 0.02, -0.04, 0.01, 0.03]
    df = pd.DataFrame({
        "EPS Estimate": [1.0 + 0.02 * i for i in range(12)] + [1.5],
        "Reported EPS": [1.0 + 0.02 * i + s for i, s in enumerate(hist_surprises)] + [1.5 + 0.30],
        "Surprise(%)": [0.0] * 13,
    }, index=pd.DatetimeIndex(list(dates), name="Earnings Date"))
    monkeypatch.setattr(et, "_load_us_holdings", lambda: [{"ticker": "AAPL", "market": "us"}])
    monkeypatch.setattr(et, "_ticker_earnings_dates_with_retry", lambda t, limit=20: df)
    monkeypatch.setattr(et, "_today_date", lambda: _dt.date(2026, 7, 1))
    asyncio.run(et.record_sue_events(db_path=db))
    rows = tk.get_sue_alerts("AAPL", db_path=db)
    assert len(rows) == 1 and rows[0]["earnings_date"] == "2026-06-30"
    assert rows[0]["sue_score"] is not None


def test_record_sue_events_skips_stale_earnings(tmp_path, monkeypatch):
    db = tmp_path / "t.db"; tk.init_db(db)
    dates = pd.date_range("2023-06-01", periods=13, freq="90D", tz="US/Eastern")
    df = pd.DataFrame({
        "EPS Estimate": [1.0 + 0.02 * i for i in range(13)],
        "Reported EPS": [1.0 + 0.02 * i + 0.30 for i in range(13)],
        "Surprise(%)": [0.0] * 13,
    }, index=pd.DatetimeIndex(list(dates), name="Earnings Date"))
    monkeypatch.setattr(et, "_load_us_holdings", lambda: [{"ticker": "AAPL", "market": "us"}])
    monkeypatch.setattr(et, "_ticker_earnings_dates_with_retry", lambda t, limit=20: df)
    monkeypatch.setattr(et, "_today_date", lambda: _dt.date(2026, 7, 1))
    asyncio.run(et.record_sue_events(db_path=db))
    assert tk.get_sue_alerts("AAPL", db_path=db) == []

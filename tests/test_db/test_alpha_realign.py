# tests/test_db/test_alpha_realign.py
"""order1：alpha 双腿配对窗口对齐 + realign 迁移测试（mock yfinance）。"""
import pandas as pd
import pytest

from finance_agent.db import tracker


def _mk_df(closes, start="2026-05-11"):
    idx = pd.date_range(start, periods=len(closes), freq="D")
    return pd.DataFrame({"Close": closes}, index=idx)


def test_paired_window(monkeypatch):
    df = _mk_df([100, 101, 102, 103, 104, 105, 106, 107, 108, 109])
    monkeypatch.setattr(tracker.yf, "download", lambda *a, **k: df)
    p0, p_exit, exit_date, n = tracker._fetch_paired_window("NVDA", "us", "2026-05-11", fwd_td=7)
    assert p0 == 100.0 and p_exit == 107.0 and n == 7      # 第0→第7交易日
    assert exit_date == "2026-05-18"


def test_paired_window_short_series(monkeypatch):
    # 不足 7 交易日（遇长假）→ 用实际最后一根，exit_idx=len-1
    df = _mk_df([100, 102, 104])
    monkeypatch.setattr(tracker.yf, "download", lambda *a, **k: df)
    p0, p_exit, exit_date, n = tracker._fetch_paired_window("X", "us", "2026-05-11", fwd_td=7)
    assert p0 == 100.0 and p_exit == 104.0 and n == 2


def test_benchmark_window_aligns_to_exit(monkeypatch):
    df = _mk_df([200, 201, 202, 203, 204, 205, 206, 207])
    monkeypatch.setattr(tracker.yf, "download", lambda *a, **k: df)
    bm = tracker._fetch_benchmark_window("us", "2026-05-11", "2026-05-18")  # 第7根=207
    assert bm == 3.5   # (207-200)/200


@pytest.mark.asyncio
async def test_realign_dry_run_then_apply(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    tracker.init_db(db)
    # 旧伪 alpha 行：ret=-2.0 bm=+1.0（错位窗口产物）
    with tracker._conn(db) as con:
        con.execute(
            "INSERT INTO recommendations(date,ticker,recommendation,position_change,"
            "price_at_rec,price_7d,return_7d,benchmark_return_7d,market,outcome) "
            "VALUES('2026-05-11','VOO','持有','维持',500,490,-2.0,1.0,'us','错误')"
        )
    df = _mk_df([500, 501, 502, 503, 504, 505, 506, 507])  # 同窗 +1.4%（个股=基准，VOO跟踪标普 alpha≈0）
    monkeypatch.setattr(tracker.yf, "download", lambda *a, **k: df)

    # dry-run：检测到变更但不写库
    res = await tracker.realign_alpha(db_path=db, dry_run=True)
    assert res["changed"] == 1 and res["dry_run"] is True
    s = res["samples"][0]
    assert s["old_ret"] == -2.0 and s["new_ret"] == 1.4
    assert s["old_alpha"] == -3.0 and abs(s["new_alpha"]) < 0.01   # 伪 -3% → 真≈0
    with tracker._conn(db) as con:
        assert con.execute("SELECT return_7d FROM recommendations").fetchone()["return_7d"] == -2.0  # 未写

    # apply：真正写库
    res2 = await tracker.realign_alpha(db_path=db, dry_run=False)
    assert res2["changed"] == 1
    with tracker._conn(db) as con:
        r = con.execute("SELECT return_7d, benchmark_return_7d FROM recommendations").fetchone()
    assert r["return_7d"] == 1.4 and r["benchmark_return_7d"] == 1.4   # 两腿同窗，alpha≈0

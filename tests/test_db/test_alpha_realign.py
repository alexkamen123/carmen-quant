# tests/test_db/test_alpha_realign.py
"""order1：alpha 双腿配对窗口对齐 + realign 迁移测试（mock yfinance）。"""
import pandas as pd
import pytest

from finance_agent.db import tracker


def _mk_df(closes, start="2026-05-11"):
    idx = pd.date_range(start, periods=len(closes), freq="D")
    return pd.DataFrame({"Close": closes}, index=idx)


def _seed_row(db, ret=-2.0, bm=1.0):
    """种一行旧伪 alpha 记录（错位窗口产物）：ret=-2.0 bm=+1.0"""
    with tracker._conn(db) as con:
        con.execute(
            "INSERT INTO recommendations(date,ticker,recommendation,position_change,"
            "price_at_rec,price_7d,return_7d,benchmark_return_7d,market,outcome) "
            "VALUES('2026-05-11','VOO','持有','维持',500,490,?,?,'us','错误')",
            (ret, bm),
        )


def test_paired_window(monkeypatch):
    df = _mk_df([100, 101, 102, 103, 104, 105, 106, 107, 108, 109])
    monkeypatch.setattr(tracker.yf, "download", lambda *a, **k: df)
    p0, p_exit, start_date, exit_date, last_idx = tracker._fetch_paired_window("NVDA", "us", "2026-05-11", fwd_td=7)
    assert p0 == 100.0 and p_exit == 107.0    # 第0→第7交易日
    assert last_idx == 9                       # 最后一根 bar 序号（供盘中半根防护判断）
    assert start_date == "2026-05-11" and exit_date == "2026-05-18"


def test_paired_window_short_series(monkeypatch):
    # 不足 7 交易日（遇长假）→ 用实际最后一根，exit_idx=len-1
    df = _mk_df([100, 102, 104])
    monkeypatch.setattr(tracker.yf, "download", lambda *a, **k: df)
    p0, p_exit, start_date, exit_date, last_idx = tracker._fetch_paired_window("X", "us", "2026-05-11", fwd_td=7)
    assert p0 == 100.0 and p_exit == 104.0 and last_idx == 2


def test_paired_window_dirty_price(monkeypatch):
    # 脏数据（0/负复权价）当作取数失败丢弃，不许算出 -100% 级假跌幅
    monkeypatch.setattr(tracker.yf, "download",
                        lambda *a, **k: _mk_df([100, 101, 102, 103, 104, 105, 106, 0.0]))
    assert tracker._fetch_paired_window("X", "us", "2026-05-11", fwd_td=7) is None
    monkeypatch.setattr(tracker.yf, "download",
                        lambda *a, **k: _mk_df([0.0, 101, 102]))
    assert tracker._fetch_paired_window("X", "us", "2026-05-11", fwd_td=7) is None


def test_benchmark_window_aligns_to_exit(monkeypatch):
    df = _mk_df([200, 201, 202, 203, 204, 205, 206, 207])
    monkeypatch.setattr(tracker.yf, "download", lambda *a, **k: df)
    bm = tracker._fetch_benchmark_window("us", "2026-05-11", "2026-05-18")  # 第7根=207
    assert bm == 3.5   # (207-200)/200


def test_benchmark_window_start_asof(monkeypatch):
    # 个股 rec 日（05-11）停牌、真实起点漂到 05-12 → 基准腿 b0 必须 asof 对齐到 05-12
    full = _mk_df([200, 202, 204, 206, 208, 210, 212, 214], start="2026-05-11")

    def fake_dl(tkr, start=None, **k):
        return full[full.index >= pd.Timestamp(start)]   # 尊重 start 参数（yf.download 语义）

    monkeypatch.setattr(tracker.yf, "download", fake_dl)
    bm = tracker._fetch_benchmark_window("us", "2026-05-12", "2026-05-18")
    assert bm == round((214 - 202) / 202 * 100, 2)        # b0=202(05-12)，非 200(05-11)


@pytest.mark.asyncio
async def test_realign_dry_run_then_apply(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    tracker.init_db(db)
    _seed_row(db)
    df = _mk_df([500, 501, 502, 503, 504, 505, 506, 507])  # 同窗 +1.4%（个股=基准，VOO跟踪标普 alpha≈0）
    monkeypatch.setattr(tracker.yf, "download", lambda *a, **k: df)

    # dry-run：检测到变更但不写库
    res = await tracker.realign_alpha(db_path=db, dry_run=True)
    assert res["changed"] == 1 and res["dry_run"] is True and res["failed"] == 0
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


@pytest.mark.asyncio
async def test_realign_benchmark_fail_keeps_old(tmp_path, monkeypatch):
    """基准腿失败 → 整行原子跳过：绝不把有效 benchmark 抹成 NULL、不混搭新旧两腿。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    _seed_row(db)
    stock_df = _mk_df([500, 501, 502, 503, 504, 505, 506, 507])

    def fake_dl(tkr, *a, **k):
        return stock_df if tkr == "VOO" else pd.DataFrame()   # 基准（SPY）拉取失败

    monkeypatch.setattr(tracker.yf, "download", fake_dl)
    res = await tracker.realign_alpha(db_path=db, dry_run=False)
    assert res["changed"] == 0 and res["failed"] == 1
    assert res["failed_samples"][0]["reason"] == "no_benchmark"
    with tracker._conn(db) as con:
        r = con.execute("SELECT return_7d, benchmark_return_7d FROM recommendations").fetchone()
    assert r["return_7d"] == -2.0 and r["benchmark_return_7d"] == 1.0  # 旧值原样保留


@pytest.mark.asyncio
async def test_realign_stock_fail_counted(tmp_path, monkeypatch):
    """个股腿失败 → failed 计数 + reason=no_window，行不动。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    _seed_row(db)
    monkeypatch.setattr(tracker.yf, "download", lambda *a, **k: pd.DataFrame())
    res = await tracker.realign_alpha(db_path=db, dry_run=False)
    assert res["changed"] == 0 and res["failed"] == 1
    assert res["failed_samples"][0]["reason"] == "no_window"
    with tracker._conn(db) as con:
        r = con.execute("SELECT return_7d, benchmark_return_7d FROM recommendations").fetchone()
    assert r["return_7d"] == -2.0 and r["benchmark_return_7d"] == 1.0

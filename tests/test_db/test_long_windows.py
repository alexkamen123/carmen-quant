# tests/test_db/test_long_windows.py
"""order3：30/90 日窗口回填（fill_long_returns）+ recommendations 迁移测试（mock yfinance）。"""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from finance_agent.db import tracker

_NEW_COLS = ("price_30d", "return_30d", "benchmark_return_30d",
             "price_90d", "return_90d", "benchmark_return_90d")

_OLD_REC_DDL = """
CREATE TABLE recommendations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    recommendation  TEXT,
    position_change TEXT,
    price_at_rec    REAL,
    price_7d        REAL,
    return_7d       REAL,
    outcome         TEXT
);
"""


def _days_ago(n: int) -> str:
    return (datetime.today() - timedelta(days=n)).strftime("%Y-%m-%d")


def _mk_df(closes, start):
    idx = pd.date_range(start, periods=len(closes), freq="D")
    return pd.DataFrame({"Close": closes}, index=idx)


def _seed(db, date, ticker="NVDA", **extra):
    cols = ["date", "ticker", "recommendation", "position_change", "price_at_rec", "market"]
    vals = [date, ticker, "持有", "维持", 100.0, "us"]
    for k, v in extra.items():
        cols.append(k)
        vals.append(v)
    with tracker._conn(db) as con:
        con.execute(
            f"INSERT INTO recommendations({','.join(cols)}) "
            f"VALUES({','.join('?' * len(vals))})", vals,
        )


def test_migration_idempotent(tmp_path):
    """旧 shape 表 + 一行 → init_db 跑两次 → 6 新列齐全、旧行新列全 NULL。"""
    db = tmp_path / "t.db"
    with tracker._conn(db) as con:
        con.executescript(_OLD_REC_DDL)
        con.execute(
            "INSERT INTO recommendations(date, ticker, recommendation) "
            "VALUES('2026-05-11', 'NVDA', '持有')"
        )
    tracker.init_db(db)
    tracker.init_db(db)
    with tracker._conn(db) as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info(recommendations)").fetchall()}
        assert set(_NEW_COLS) <= cols
        row = con.execute("SELECT * FROM recommendations").fetchone()
    for c in _NEW_COLS:
        assert row[c] is None


@pytest.mark.asyncio
async def test_fill_30d_happy(tmp_path, monkeypatch):
    """30 日窗口成熟（25 根 ≥ 21）→ 按第 0→21 根写入，7d 列不被触碰。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    rec_date = _days_ago(40)
    _seed(db, rec_date, return_7d=5.0)
    df = _mk_df([100 + i for i in range(25)], start=rec_date)   # 100→124
    monkeypatch.setattr(tracker.yf, "download", lambda *a, **k: df)

    res = await tracker.fill_long_returns(db_path=db)
    assert res == {"filled": 1, "immature": 0, "failed": 0}
    with tracker._conn(db) as con:
        r = con.execute("SELECT * FROM recommendations").fetchone()
    assert r["price_30d"] == 121.0                     # 第 21 根
    assert r["return_30d"] == 21.0                     # (121-100)/100
    assert r["benchmark_return_30d"] == 21.0           # 同 df → 同窗同涨幅
    assert r["return_7d"] == 5.0                       # 7d 列未动
    assert r["return_90d"] is None                     # 90d cutoff 未到，不回填


@pytest.mark.asyncio
async def test_fill_30d_immature(tmp_path, monkeypatch):
    """窗口未走满（15 根 < 21 交易日）→ 整行跳过保持 NULL，下轮可重试。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    rec_date = _days_ago(40)
    _seed(db, rec_date)
    monkeypatch.setattr(tracker.yf, "download",
                        lambda *a, **k: _mk_df([100 + i for i in range(15)], start=rec_date))

    res = await tracker.fill_long_returns(db_path=db)
    assert res == {"filled": 0, "immature": 1, "failed": 0}
    with tracker._conn(db) as con:
        r = con.execute("SELECT * FROM recommendations").fetchone()
    for c in _NEW_COLS:
        assert r[c] is None


@pytest.mark.asyncio
async def test_fill_atomic_bm_fail(tmp_path, monkeypatch):
    """基准腿失败 → 原子整行不写（防 return 有值 bm NULL 被永久 strand）。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    rec_date = _days_ago(40)
    _seed(db, rec_date, ticker="NVDA")
    stock_df = _mk_df([100 + i for i in range(25)], start=rec_date)

    def fake_dl(tkr, *a, **k):
        return stock_df if tkr == "NVDA" else pd.DataFrame()   # SPY 失败

    monkeypatch.setattr(tracker.yf, "download", fake_dl)
    res = await tracker.fill_long_returns(db_path=db)
    assert res == {"filled": 0, "immature": 0, "failed": 1}
    with tracker._conn(db) as con:
        r = con.execute("SELECT * FROM recommendations").fetchone()
    for c in _NEW_COLS:
        assert r[c] is None


@pytest.mark.asyncio
async def test_fill_90d_dormant(tmp_path, monkeypatch):
    """90d cutoff 预过滤生效：30d 已回填、90d 窗口未到 → 全零计数且从不触网。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    _seed(db, _days_ago(40), return_30d=10.0, price_30d=110.0, benchmark_return_30d=8.0)

    def boom(*a, **k):
        raise AssertionError("不该触网")

    monkeypatch.setattr(tracker.yf, "download", boom)
    res = await tracker.fill_long_returns(db_path=db)
    assert res == {"filled": 0, "immature": 0, "failed": 0}


@pytest.mark.asyncio
async def test_fill_exit_bar_is_last_immature(tmp_path, monkeypatch):
    """exit bar 恰为最后一根（22 根，last_idx==fwd_td）→ 可能是当日盘中半根，
    判 immature 多等一个交易日，防把盘中价永久写死（30/90 无 realign 兜底）。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    rec_date = _days_ago(40)
    _seed(db, rec_date)
    monkeypatch.setattr(tracker.yf, "download",
                        lambda *a, **k: _mk_df([100 + i for i in range(22)], start=rec_date))

    res = await tracker.fill_long_returns(db_path=db)
    assert res == {"filled": 0, "immature": 1, "failed": 0}
    with tracker._conn(db) as con:
        assert con.execute("SELECT return_30d FROM recommendations").fetchone()["return_30d"] is None


@pytest.mark.asyncio
async def test_fill_permanent_pending_floor(tmp_path, monkeypatch):
    """超过固定下载窗口+宽限（30d: 63 天）仍 NULL 的行 = 永久不可回填，退出 pending 不再触网。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    _seed(db, _days_ago(70), ticker="DEAD")   # 退市票：70 > 21*2+14+7=63

    def boom(*a, **k):
        raise AssertionError("永久死行不该再触网")

    monkeypatch.setattr(tracker.yf, "download", boom)
    res = await tracker.fill_long_returns(db_path=db)
    assert res == {"filled": 0, "immature": 0, "failed": 0}


@pytest.mark.asyncio
async def test_fill_dirty_price(tmp_path, monkeypatch):
    """exit 价为 0（停牌/脏复权）→ 源头当取数失败，failed 计数、不写库。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    rec_date = _days_ago(40)
    _seed(db, rec_date)
    closes = [100.0 + i for i in range(25)]
    closes[21] = 0.0   # 第 21 根（exit）脏数据
    monkeypatch.setattr(tracker.yf, "download", lambda *a, **k: _mk_df(closes, start=rec_date))

    res = await tracker.fill_long_returns(db_path=db)
    assert res == {"filled": 0, "immature": 0, "failed": 1}
    with tracker._conn(db) as con:
        r = con.execute("SELECT return_30d FROM recommendations").fetchone()
    assert r["return_30d"] is None

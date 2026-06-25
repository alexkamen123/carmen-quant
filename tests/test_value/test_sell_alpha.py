# tests/test_value/test_sell_alpha.py
"""卖侧超额单列：让「合计=买+卖」的负值有可见出处。

用户审计发现：卡片显示「让你买的 +、让你拿住的 +、合计 -」像自相矛盾，
根因是「让你卖减的」只显示卖对/卖飞计数、没有 alpha 数，合计的负值凭空冒出。
修复=单算卖侧 alpha（bearish 反号：卖后跌=正贡献、卖飞=负），三行可见地组合成合计。
"""
from finance_agent.db import tracker
from finance_agent.value.metrics import compute_value_metrics


def _ins(con, date, ticker, rec, ret7, bm7):
    con.execute(
        "INSERT INTO recommendations(date,ticker,recommendation,position_change,"
        "price_at_rec,market,return_7d,benchmark_return_7d,outcome) VALUES(?,?,?,?,?,?,?,?,?)",
        (date, ticker, rec, "", 100.0, "us", ret7, bm7, "回填"),
    )


def test_sell_alpha_bearish_signed():
    db = "/tmp/_sa_test.db"
    import os
    if os.path.exists(db):
        os.remove(db)
    from pathlib import Path
    db = Path(db)
    tracker.init_db(db)
    with tracker._conn(db) as con:
        _ins(con, "2026-05-01", "AAA", "减仓", 20.0, 2.0)   # 卖飞：卖后涨18 → 反号 -18
        _ins(con, "2026-05-10", "BBB", "减仓", -10.0, 1.0)  # 躲跌：卖后跌 → 反号 +11
    m = compute_value_metrics(db)
    sa = m["sell_alpha"]
    assert sa["n"] == 2
    assert abs(sa["avg"] - (-3.5)) < 0.01      # (-18 + 11)/2 = -3.5
    assert sa["status"] == "ok"


def test_sell_alpha_no_sample():
    from pathlib import Path
    import os
    db = "/tmp/_sa_test2.db"
    if os.path.exists(db):
        os.remove(db)
    db = Path(db)
    tracker.init_db(db)
    with tracker._conn(db) as con:
        _ins(con, "2026-05-01", "AAA", "买入", 5.0, 1.0)    # 只有买，无卖
    m = compute_value_metrics(db)
    assert m["sell_alpha"]["n"] == 0
    assert m["sell_alpha"]["status"] == "no_sample"

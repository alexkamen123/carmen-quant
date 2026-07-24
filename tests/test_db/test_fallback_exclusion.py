# tests/test_db/test_fallback_exclusion.py
"""P2c：is_fallback=1（异常兜底观望）不得污染"持有判断质量"统计（零网络）。"""
from finance_agent.db import tracker
from finance_agent.value.metrics import compute_value_metrics


def _seed(db, ticker, rec, ret, bm, is_watch=0, is_fallback=0, pos=None, date="2026-06-01"):
    with tracker._conn(db) as con:
        con.execute(
            "INSERT INTO recommendations(date, ticker, recommendation, position_change, "
            "return_7d, benchmark_return_7d, market, is_watch, is_fallback) "
            "VALUES(?,?,?,?,?,?,'us',?,?)",
            (date, ticker, rec, pos, ret, bm, is_watch, is_fallback),
        )


def test_fallback_hold_rows_excluded_from_hold_quality(tmp_path):
    db = tmp_path / "t.db"
    tracker.init_db(db)
    _seed(db, "A", "观望", 5.0, 1.0, is_fallback=0)   # 真实观望，跑赢 → 计入
    _seed(db, "B", "观望", -8.0, -1.0, is_fallback=1)  # 异常兜底观望 → 排除
    m = compute_value_metrics(db)
    h = m["hold_quality"]
    assert h["n"] == 1
    assert h["wrong_cases"] == [] or all(c["ticker"] != "B" for c in h["wrong_cases"])

# tests/test_value/test_verdict_hold.py
"""verdict「准不准」总览必须覆盖【全部建议】——买卖建议 + 持有建议，而非只数买卖。

用户主张：系统每天给每只票的「持有别动」也是建议、也有对错(拿住跑赢=对、该减没减=错)，
凭什么只把买卖算进准确率？修复：verdict 标题升格「我们的建议」准不准，分两类各报准确率，
买卖用方向命中、持有用 vs 大盘 alpha——口径不同不混算，但都纳入总览。
"""
from finance_agent.db import tracker
from finance_agent.value.metrics import compute_value_metrics


def _ins(con, date, ticker, rec, pc, ret, bm):
    con.execute(
        "INSERT INTO recommendations(date,ticker,recommendation,position_change,"
        "return_7d,benchmark_return_7d,market,is_watch,outcome) VALUES(?,?,?,?,?,?,?,0,?)",
        (date, ticker, rec, pc, ret, bm, "us", None if ret is None else "回填"),
    )


def test_verdict_covers_hold_advice(tmp_path):
    db = tmp_path / "t.db"
    tracker.init_db(db)
    with tracker._conn(db) as con:
        _ins(con, "2026-06-01", "AAPL", "持有", "维持", 5.0, 1.0)    # alpha +4 → 拿住对
        _ins(con, "2026-06-01", "MSFT", "持有", "维持", -8.0, 1.0)   # alpha -9 → 该减没减
        _ins(con, "2026-06-01", "GOOG", "持有", "维持", 0.5, 1.0)    # alpha -0.5 → 中性(−5~0)
        _ins(con, "2026-06-02", "NVDA", "买入", "小加1", 5.0, 1.0)   # 买卖，gate 未过
    m = compute_value_metrics(db)
    txt = m["verdict"]["text"]
    assert m["verdict"]["state"] == 0            # gate 未过仍灰
    assert m["hold_quality"]["right"] == 1 and m["hold_quality"]["wrong"] == 1
    # 持有建议被纳入「准不准」总览
    assert "持有建议" in txt
    assert "3 条" in txt                          # 持有 3 条
    assert "拿住" in txt                          # 拿住对/该减没减
    # 标题升格：覆盖全部建议，不再只说买卖
    assert "我们的建议" in txt

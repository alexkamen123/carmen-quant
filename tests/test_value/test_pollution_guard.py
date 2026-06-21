# tests/test_value/test_pollution_guard.py
"""阶段0 止血回归：两处口径污染必须被堵死。
① 「持有/观望 + position_change=小加/大加」语义是持有，不得计为方向性买入信号
   （实测 00700 三条此类行曾灌进 buy_alpha，其中 +10.07 outlier 主导对外头条）。
② hold_quality 的 hold_rows 必须按 (票, ISO周) 去重，防单票日频灌爆分母
   （实测 00700×23 / GOOGL×23 等未去重）。
"""
from finance_agent.db import tracker
from finance_agent.value.metrics import compute_value_metrics, _is_bullish


def _insert(con, date, ticker, rec, pc, ret, bm, is_watch=0):
    con.execute(
        "INSERT INTO recommendations(date,ticker,recommendation,position_change,"
        "return_7d,benchmark_return_7d,market,is_watch,outcome) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (date, ticker, rec, pc, ret, bm, "us", is_watch,
         None if ret is None else "回填"),
    )


def test_hold_with_add_pc_not_bullish():
    """单元层：_is_bullish 对『持有/观望 + 小加』恒为 False（rec 优先于 pc）。"""
    assert _is_bullish("持有", "小加（+5~10%）") is False
    assert _is_bullish("观望", "大加（+10%）") is False
    # 真买入仍判 True，不误伤
    assert _is_bullish("买入", "小加（+5~10%）") is True
    assert _is_bullish("买入", "维持") is True


def test_hold_add_pc_excluded_from_buy_alpha(tmp_path):
    """集成层：『持有+小加』高 alpha 行不得进 buy_alpha，也不得进方向性 dir 样本。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    with tracker._conn(db) as con:
        # 污染行：持有却标小加，+10% 巨幅超额（若漏判会翻转买入头条）
        _insert(con, "2026-06-01", "00700", "持有", "小加（+5~10%）", 6.79, -3.28)
        # 一条真买入做对照
        _insert(con, "2026-06-05", "FUTU", "买入", "小加（+5~10%）", 5.0, 1.0)
    m = compute_value_metrics(db)
    # buy_alpha 只应包含 FUTU 这一条真买入；污染行被排除
    assert m["buy_alpha"]["n"] == 1, m["buy_alpha"]
    assert abs(m["buy_alpha"]["avg"] - 4.0) < 1e-6   # 5.0-1.0，未被 00700 的 +10 污染
    # 污染行也不得计入方向性闸门量
    assert m["gate"]["n_buy"] == 1


def test_hold_rows_deduped_weekly(tmp_path):
    """集成层：同票同 ISO 周的多条持有只算一次，防分母灌爆。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    with tracker._conn(db) as con:
        # 同一 ISO 周内 00700 连续 5 天『持有』——应折叠成 1 条
        for d in ("2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05"):
            _insert(con, d, "00700", "持有", "维持", 1.5, 0.5)
    m = compute_value_metrics(db)
    assert m["hold_quality"]["n"] == 1, m["hold_quality"]

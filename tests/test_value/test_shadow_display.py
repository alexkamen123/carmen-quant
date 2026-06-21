# tests/test_value/test_shadow_display.py
"""阶段2：影子选股轨展示 —— 只展示明细+Wilson CI，措辞固定「样本不足不下结论」，
含挑票偏差声明，绝不出现「证明了」式结论，绝不并入真实账户口径。"""
from finance_agent.db import tracker
from finance_agent.value.metrics import compute_value_metrics
from finance_agent.value.report import _shadow_section


def _seed_shadow(con, rows):
    for d, t, rec, ret, bm in rows:
        con.execute(
            "INSERT INTO recommendations(date,ticker,recommendation,position_change,"
            "return_7d,benchmark_return_7d,market,is_watch,outcome) "
            "VALUES(?,?,?,?,?,?,?,1,?)",
            (d, t, rec, None, ret, bm, "us", None if ret is None else "回填"),
        )


def test_shadow_display_no_conclusion_wording(tmp_path):
    db = tmp_path / "t.db"
    tracker.init_db(db)
    with tracker._conn(db) as con:
        _seed_shadow(con, [
            ("2026-06-01", "GS",  "买入", 8.0, 2.0),    # 对
            ("2026-06-02", "PLTR", "买入", -4.0, 1.0),  # 错
        ])
    m = compute_value_metrics(db)
    s = m["shadow_picks"]
    # n = 已回填可测量的影子数（未满 7 天的不计入，符合既有口径）
    assert s["n"] == 2 and s["n_judged"] == 2 and s["correct"] == 1
    assert s["ci_low"] is not None and s["ci_high"] is not None   # Wilson CI 在
    txt = _shadow_section(m)
    # 固定「样本不足不下结论」基调，永不出现确定性结论措辞
    assert "样本不足" in txt
    assert "挑票偏差" in txt and "不可外推" in txt
    assert "证明了" not in txt and "证明我们" not in txt
    # 明细可见
    assert "GS" in txt and "PLTR" in txt


def test_shadow_not_mixed_into_operation_track(tmp_path):
    """影子方向裁决绝不并入操作轨 combined_alpha / gate / cumulative。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    with tracker._conn(db) as con:
        _seed_shadow(con, [("2026-06-01", "GS", "买入", 8.0, 2.0)])
    m = compute_value_metrics(db)
    assert m["gate"]["n_dir"] == 0
    assert m["combined_alpha"]["n"] == 0
    assert m["shadow_picks"]["n"] == 1

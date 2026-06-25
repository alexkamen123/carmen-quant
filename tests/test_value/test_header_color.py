# tests/test_value/test_header_color.py
"""阶段1a 颜色脱钩：整张卡的 header 颜色由 cumulative「你 vs 躺平」驱动，
不再被防 p-hacking 的样本闸门焊死成永久灰态。

病灶：header template = verdict.color，而 verdict 因 GATE_MIN_N=30 几乎永远撞不到
→ 永远 grey。修复：只要有真实账户累计(cumulative)，header 由 excess 正负定色，
北极星朴素问题「听我们的 vs 躺平多赚多少钱」永远有答案；cumulative 缺失才回落 verdict.color。
"""
from finance_agent.db import tracker
from finance_agent.value.metrics import compute_value_metrics
from finance_agent.value.report import build_value_card


def _seed_buys(db, rows):
    tracker.init_db(db)
    with tracker._conn(db) as con:
        for i, (t, ret, bm) in enumerate(rows):
            con.execute(
                "INSERT INTO recommendations(date,ticker,recommendation,position_change,"
                "return_7d,benchmark_return_7d,market,outcome) VALUES(?,?,?,?,?,?,?,?)",
                (f"2026-05-{10 + i:02d}", t, "买入", "小加1", ret, bm, "us", "回填"),
            )


_CUM = {"as_of": "2026-06-21", "basis": "real", "n_positions": 3,
        "strategy_cum_pct": 2.9, "passive_cum_pct": -1.8, "excess_pct": 4.7,
        "excess_amount_usd": 251.0, "partial": []}


def test_cumulative_positive_makes_header_green_despite_grey_verdict(tmp_path):
    db = tmp_path / "t.db"
    _seed_buys(db, [("NVDA", 5, 1), ("MU", 6, 1)])     # n_dir=2<30 → verdict 灰
    m = compute_value_metrics(db)
    assert m["verdict"]["color"] == "grey"             # 前提：verdict 确实灰
    m["cumulative"] = dict(_CUM)                        # 但账户真实跑赢躺平 +4.7%
    assert build_value_card(m)["header"]["template"] == "green"


def test_cumulative_negative_makes_header_orange(tmp_path):
    db = tmp_path / "t.db"
    _seed_buys(db, [("NVDA", 5, 1), ("MU", 6, 1)])
    m = compute_value_metrics(db)
    m["cumulative"] = {**_CUM, "excess_pct": -2.0, "excess_amount_usd": -99.0}
    assert build_value_card(m)["header"]["template"] == "orange"


def test_no_cumulative_falls_back_to_verdict_color(tmp_path):
    db = tmp_path / "t.db"
    _seed_buys(db, [("NVDA", 5, 1), ("MU", 6, 1)])
    m = compute_value_metrics(db)
    m["cumulative"] = None
    assert build_value_card(m)["header"]["template"] == "grey"  # 回落 verdict.color

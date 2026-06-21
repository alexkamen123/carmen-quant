# tests/test_db/test_hold_quality.py
"""task#29：持有判断质量分栏（A）+ 影子选股分栏隔离（B1）测试（零网络）。"""
from finance_agent.db import tracker
from finance_agent.value.metrics import compute_value_metrics, HOLD_BAD_ALPHA
from finance_agent.value.report import _adv_section, _shadow_section


def _seed(db, ticker, rec, ret, bm, is_watch=0, pos=None, date="2026-06-01"):
    with tracker._conn(db) as con:
        con.execute(
            "INSERT INTO recommendations(date, ticker, recommendation, position_change, "
            "return_7d, benchmark_return_7d, market, is_watch) VALUES(?,?,?,?,?,?,'us',?)",
            (date, ticker, rec, pos, ret, bm, is_watch),
        )


def test_hold_quality_buckets(tmp_path):
    """跑赢=对；跑输<=-5%=错；区间内=中性；定投/方向行不进此栏。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    _seed(db, "A", "持有", 5.0, 1.0)     # alpha +4 → 对
    _seed(db, "B", "持有", -8.0, -1.0)   # alpha -7 → 错（该减没减）
    _seed(db, "C", "观望", -2.0, 1.0)    # alpha -3 → 中性（>-5）
    _seed(db, "D", "按计划定投", -9.0, 0.0)  # 定投不是判断，不进
    _seed(db, "E", "买入", 9.0, 0.0)     # 方向行，不进持有栏
    m = compute_value_metrics(db)
    h = m["hold_quality"]
    assert h == {"n": 3, "right": 1, "wrong": 1, "neutral": 1, "avg_alpha": -2.0,
                 "bad_alpha_threshold": HOLD_BAD_ALPHA,
                 "wrong_cases": [{"date": "2026-06-01", "ticker": "B", "alpha": -7.0}]}
    # 持有质量现渲染在能力总评第2问（loop③：死代码已删、明细提主屏）
    out = _adv_section(m)
    assert "幸亏拿住(跑赢) **1**" in out and "B(06-01 差7%)" in out


def test_hold_quality_not_mixed_into_hit_rate(tmp_path):
    """红线：持有行绝不进方向命中率分子分母（分栏不混算）。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    for i in range(10):
        _seed(db, f"T{i}", "持有", 8.0, 0.0)   # 10 条全跑赢的持有
    _seed(db, "X", "买入", -5.0, 0.0)          # 1 条买错
    m = compute_value_metrics(db)
    assert m["hit_rate"]["n_judged"] == 1 and m["hit_rate"]["wrong"] == 1
    assert m["hold_quality"]["n"] == 10        # 各归各栏


def test_shadow_rows_isolated(tmp_path):
    """影子行（is_watch=1）：进影子栏、绝不进操作命中率/持有栏。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    _seed(db, "AVGO", "买入", 6.0, 1.0, is_watch=1)    # 影子判对，alpha +5
    _seed(db, "KLAC", "买入", -4.0, 1.0, is_watch=1)   # 影子判错
    _seed(db, "PLTR", "持有", 9.0, 0.0, is_watch=1)    # 影子持有 → 哪栏都不进统计主体
    _seed(db, "MU", "减仓", -6.0, 0.0, is_watch=0)     # 真实建议判对
    m = compute_value_metrics(db)
    assert m["hit_rate"]["n_judged"] == 1 and m["hit_rate"]["correct"] == 1
    assert m["hold_quality"]["n"] == 0
    s = m["shadow_picks"]
    assert s["n"] == 2 and s["correct"] == 1 and s["wrong"] == 1
    assert s["avg_alpha"] == 0.0   # (+5 + -5)/2
    assert "选股眼光·影子轨" in _shadow_section(m)


def test_shadow_section_hidden_when_empty(tmp_path):
    db = tmp_path / "t.db"
    tracker.init_db(db)
    m = compute_value_metrics(db)
    assert _shadow_section(m) == ""


def test_save_recommendations_is_watch_roundtrip(tmp_path):
    """save 路径写入 is_watch；缺省 0；史前行迁移后为 NULL。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    tracker.save_recommendations("2026-06-11", [
        {"ticker": "AVGO", "recommendation": "买入", "is_watch": 1},
        {"ticker": "MU", "recommendation": "持有"},
    ], db_path=db)
    with tracker._conn(db) as con:
        rows = {r["ticker"]: r["is_watch"] for r in
                con.execute("SELECT ticker, is_watch FROM recommendations").fetchall()}
    assert rows == {"AVGO": 1, "MU": 0}

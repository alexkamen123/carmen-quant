"""P2b 扩展：SUE 漂移 edge 影子测量测试（纯观测·脱网·tmp_path DB）。"""
from finance_agent.db import tracker
from finance_agent.value.sue_edge import sue_edge_reading


def _set_outcome(db, ticker, earnings_date, return_30d, benchmark_return_30d):
    from finance_agent.db.tracker import _conn, _resolve_db
    with _conn(_resolve_db(db)) as con:
        con.execute(
            "UPDATE earnings_surprise_alerts SET price_30d=?, return_30d=?, benchmark_return_30d=? "
            "WHERE ticker=? AND earnings_date=?",
            (100.0, return_30d, benchmark_return_30d, ticker, earnings_date))


def _seed(db, ticker, ed, sue, ret30, bench30=1.0):
    tracker.save_sue_alert(ticker, "us", ed, sue, 2.0, 1.7, 0.1, db_path=db)
    _set_outcome(db, ticker, ed, return_30d=ret30, benchmark_return_30d=bench30)


def test_beat_edge_present_with_enough_samples(tmp_path):
    db = tmp_path / "t.db"
    tracker.init_db(db)
    # 4 条已成熟 beat 事件（earnings_date 均 >30 天早于 asof）·超额均为正
    for i in range(4):
        _seed(db, f"B{i}", f"2026-0{1 + i}-01", sue=2.0 + 0.1 * i, ret30=5.0 + i, bench30=1.0)
    rd = sue_edge_reading(db_path=db, asof="2026-07-01", min_samples=3)
    assert rd["beat"]["n"] == 4
    assert rd["beat"]["mean_excess"] > 0
    assert rd["beat"]["hit_rate"] == 1.0
    assert rd["beat"]["verdict"] == "edge_present"


def test_beat_insufficient_below_min(tmp_path):
    db = tmp_path / "t.db"
    tracker.init_db(db)
    _seed(db, "B0", "2026-01-01", sue=2.0, ret30=5.0)
    _seed(db, "B1", "2026-02-01", sue=2.2, ret30=6.0)
    rd = sue_edge_reading(db_path=db, asof="2026-07-01")  # 默认 min_samples=60
    assert rd["beat"]["n"] == 2
    assert rd["beat"]["mean_excess"] > 0        # 统计照算
    assert rd["beat"]["verdict"] == "insufficient"  # 但样本不足不下结论


def test_miss_side_drift_down(tmp_path):
    db = tmp_path / "t.db"
    tracker.init_db(db)
    # miss 事件·超额均为负（爆雷后继续跑输=避开对了）
    for i in range(3):
        _seed(db, f"M{i}", f"2026-0{1 + i}-01", sue=-2.0 - 0.1 * i, ret30=-4.0 - i, bench30=1.0)
    rd = sue_edge_reading(db_path=db, asof="2026-07-01", min_samples=3)
    assert rd["miss"]["n"] == 3
    assert rd["miss"]["mean_excess"] < 0
    assert rd["miss"]["hit_rate"] == 1.0        # 全部 excess<0 = 命中
    assert rd["miss"]["verdict"] == "edge_present"


def test_immature_and_missing_excess_excluded(tmp_path):
    db = tmp_path / "t.db"
    tracker.init_db(db)
    # 有 outcome 的成熟 beat
    _seed(db, "B0", "2026-01-01", sue=2.0, ret30=5.0)
    # 无 outcome（return_30d NULL）→ 不进 matured/不进样本
    tracker.save_sue_alert("B1", "us", "2026-02-01", 2.5, 2.0, 1.7, 0.1, db_path=db)
    # 距 asof <30 天（未成熟）即使有 outcome 也被排除
    _seed(db, "B2", "2026-06-20", sue=2.3, ret30=7.0)
    rd = sue_edge_reading(db_path=db, asof="2026-07-01", min_samples=1)
    assert rd["n_total"] == 1 and rd["beat"]["n"] == 1


def test_empty_db_insufficient(tmp_path):
    db = tmp_path / "t.db"
    tracker.init_db(db)
    rd = sue_edge_reading(db_path=db, asof="2026-07-01")
    assert rd["n_total"] == 0
    assert rd["beat"]["verdict"] == "insufficient"
    assert rd["miss"]["verdict"] == "insufficient"
    assert rd["overall_rankic"] is None

# tests/test_value/test_live_rec_returns.py
"""每条推荐「到今天」的真实涨跌 + 同期基准（终局视角，替代第7天定格判对错）。
满 X 天(默认5)才纳入——太新的推荐还没到评估期，排除以免噪声混入胜率。
entry = price_at_rec（DB 100% 填充），今日价/基准实时拉。
"""
from finance_agent.db import tracker
from finance_agent.value.cumulative import compute_live_rec_returns
from finance_agent.value.metrics import compute_value_metrics


def _ins(con, date, ticker, rec, price_at_rec, market="us"):
    con.execute(
        "INSERT INTO recommendations(date,ticker,recommendation,position_change,"
        "price_at_rec,market,return_7d,outcome) VALUES(?,?,?,?,?,?,?,?)",
        (date, ticker, rec, "", price_at_rec, market, 1.0, "回填"),
    )


def test_live_rec_to_today_with_min_days(tmp_path):
    db = tmp_path / "t.db"
    tracker.init_db(db)
    with tracker._conn(db) as con:
        _ins(con, "2026-06-01", "NVDA", "买入", 200.0)        # 满5天 → 纳入
        _ins(con, "2026-06-20", "AAPL", "买入", 150.0)        # 距今2天<5 → 排除
        _ins(con, "2026-06-01", "07709", "减仓", 100.0, "hk")  # 满5天 → 纳入
    px = {"NVDA": 220.0, "07709": 150.0}
    out = compute_live_rec_returns(
        db, today="2026-06-22", min_days=5,
        price_fn=lambda tk, mkt: px.get(tk),
        bench_fn=lambda mkt, s, e: 3.0,
    )
    # 只有满5天的两条
    assert len(out) == 2
    vals = {round(v[0], 1) for v in out.values()}
    assert vals == {10.0, 50.0}        # NVDA (220-200)/200, 07709 (150-100)/100
    assert all(v[1] == 3.0 for v in out.values())   # 至今基准


def test_live_rec_skips_failed_price(tmp_path):
    db = tmp_path / "t.db"
    tracker.init_db(db)
    with tracker._conn(db) as con:
        _ins(con, "2026-06-01", "NVDA", "买入", 200.0)
    out = compute_live_rec_returns(db, today="2026-06-22", min_days=5,
                                   price_fn=lambda tk, mkt: None,
                                   bench_fn=lambda mkt, s, e: 3.0)
    assert out == {}


def test_live_rec_bench_none_ok(tmp_path):
    """基准拉取失败不影响涨幅记录（alpha 下游自行处理 None）。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    with tracker._conn(db) as con:
        _ins(con, "2026-06-01", "NVDA", "买入", 200.0)
    out = compute_live_rec_returns(db, today="2026-06-22", min_days=5,
                                   price_fn=lambda tk, mkt: 220.0,
                                   bench_fn=lambda mkt, s, e: None)
    assert len(out) == 1
    (ret, bench), = out.values()
    assert ret == 10.0 and bench is None


def _ins_full(con, date, ticker, rec, ret7, bm7):
    con.execute(
        "INSERT INTO recommendations(date,ticker,recommendation,position_change,"
        "price_at_rec,market,return_7d,benchmark_return_7d,outcome) VALUES(?,?,?,?,?,?,?,?,?)",
        (date, ticker, rec, "", 100.0, "us", ret7, bm7, "回填"),
    )


def test_metrics_live_overrides_switch_to_terminal(tmp_path):
    """live_overrides 提供→命中率/alpha 用至今值；未覆盖的行(太新)排除。不传则沿用7天定格。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    with tracker._conn(db) as con:
        _ins_full(con, "2026-06-01", "NVDA", "买入", -5.0, 0.0)   # 7日定格:亏、跑输
        _ins_full(con, "2026-06-02", "MSFT", "买入", -3.0, 0.0)   # 7日定格:亏
        ids = {r["ticker"]: r["id"] for r in con.execute(
            "SELECT id, ticker FROM recommendations").fetchall()}

    # 终局口径：NVDA 至今 +45%(跑赢 +43)，MSFT 未覆盖(视为太新)→排除
    m = compute_value_metrics(db, live_overrides={ids["NVDA"]: (45.0, 2.0)})
    assert m["hit_rate"]["n_judged"] == 1            # 只 NVDA
    assert m["hit_rate"]["correct"] == 1             # 至今 +45 买入→对(7日本是亏)
    assert m["buy_alpha"]["n"] == 1
    assert abs(m["buy_alpha"]["avg"] - 43.0) < 0.1   # 45-2

    # 不传 override → 老7天定格逻辑，两条都在、都判亏
    m2 = compute_value_metrics(db)
    assert m2["hit_rate"]["n_judged"] == 2
    assert m2["hit_rate"]["correct"] == 0            # 两条7日都亏

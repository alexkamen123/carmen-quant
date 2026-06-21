"""价值体检 loop②：累计「你 vs 躺平·截至今天」头条（真实账户口径）。"""
from finance_agent.db import tracker
from finance_agent.value.cumulative import (
    aggregate_cumulative, compute_cumulative_value,
)


# ── 纯聚合数学 ─────────────────────────────────────────────────
def test_aggregate_math():
    positions = [
        {"ticker": "A", "principal_usd": 1000, "current_value_usd": 1200, "passive_value_usd": 1100},
        {"ticker": "B", "principal_usd": 1000, "current_value_usd": 900, "passive_value_usd": 1050},
    ]
    agg = aggregate_cumulative(positions, "2026-06-21")
    assert agg["strategy_cum_pct"] == 5.0    # (2100-2000)/2000
    assert agg["passive_cum_pct"] == 7.5     # (2150-2000)/2000
    assert agg["excess_pct"] == -2.5         # 跑输躺平 2.5%
    assert agg["excess_amount_usd"] == -50.0 # 2100-2150
    assert agg["n_positions"] == 2


def test_aggregate_empty_none():
    assert aggregate_cumulative([], "2026-06-21") is None
    assert aggregate_cumulative([{"ticker": "X"}], "2026-06-21") is None  # 无 principal


# ── 真实账户口径（mock 取价）：港股折美元 + 缺数据排除 ──
def _write_pf(tmp_path):
    pf = tmp_path / "portfolio.yaml"
    pf.write_text(
        "holdings:\n"
        "  - ticker: NVDA\n    market: us\n    shares: 2\n    cost_basis: 100.0\n"
        "  - ticker: \"00700\"\n    market: hk\n    shares: 10\n    cost_basis: 500.0\n"
        "  - ticker: FAIL\n    market: us\n    shares: 1\n    cost_basis: 50.0\n"
    )
    return pf


def test_compute_real_basis_currency_and_partial(tmp_path):
    pf = _write_pf(tmp_path)
    db = tmp_path / "t.db"
    tracker.init_db(db)
    with tracker._conn(db) as con:
        for t in ("NVDA", "00700", "FAIL"):
            con.execute("INSERT INTO user_actions(date,ticker,action,source) "
                        "VALUES('2026-06-01',?,'BUY','manual')", (t,))
    prices = {"NVDA": 120.0, "00700": 600.0, "FAIL": None}   # FAIL 拉价失败
    benches = {"us": 10.0, "hk": 5.0}                         # SPY +10% / 恒指 +5%
    agg = compute_cumulative_value(
        db, pf, today="2026-06-21",
        price_fn=lambda t, m: prices.get(t),
        bench_fn=lambda m, s, e: benches.get(m),
    )
    assert agg is not None and agg["basis"] == "real"
    assert agg["n_positions"] == 2                            # FAIL 被排除
    assert any(p["ticker"] == "FAIL" for p in agg["partial"])
    # NVDA: 本金200/市值240/躺平220；00700: 本金5000÷7.8=641.03/市值769.23/躺平673.08
    # excess_amount = (240+769.23) − (220+673.08) ≈ 116.15
    assert abs(agg["excess_amount_usd"] - 116.15) < 0.6
    assert agg["strategy_cum_pct"] > agg["passive_cum_pct"]   # 这组跑赢躺平


def test_empty_holdings_returns_none(tmp_path):
    pf = tmp_path / "p.yaml"
    pf.write_text("holdings: []\n")
    db = tmp_path / "t.db"
    tracker.init_db(db)
    assert compute_cumulative_value(db, pf, today="2026-06-21") is None


def test_entry_date_falls_back_to_inception(tmp_path):
    """某票无自己的 BUY 记录时，入场日退回组合 inception（最早 BUY 日）。"""
    pf = tmp_path / "portfolio.yaml"
    pf.write_text("holdings:\n  - ticker: ZZZ\n    market: us\n    shares: 1\n    cost_basis: 10.0\n")
    db = tmp_path / "t.db"
    tracker.init_db(db)
    with tracker._conn(db) as con:  # 只有别的票有 BUY → inception=2026-05-01
        con.execute("INSERT INTO user_actions(date,ticker,action,source) "
                    "VALUES('2026-05-01','OTHER','BUY','manual')")
    seen = {}
    def bench(m, s, e):
        seen["start"] = s
        return 3.0
    agg = compute_cumulative_value(db, pf, today="2026-06-21",
                                   price_fn=lambda t, m: 12.0, bench_fn=bench)
    assert agg["n_positions"] == 1
    assert seen["start"] == "2026-05-01"   # 退回 inception 作入场日

"""价值体检改造（愿景评估 loop 第一轮）：
- P0-4 多窗口超额（消费已回填的 30/90 天列）
- P0-3 verdict 止血：主语=信号、口径=7天定格，不再把信号超额冒充"你账户截至本期跑输躺平"
"""
from finance_agent.db import tracker
from finance_agent.value.metrics import compute_value_metrics, _score_window


def _insert_rec(con, date, ticker, rec, pc, r7, b7, r30=None, b30=None,
                r90=None, b90=None, market="us"):
    con.execute(
        "INSERT INTO recommendations(date,ticker,recommendation,position_change,"
        "return_7d,benchmark_return_7d,return_30d,benchmark_return_30d,"
        "return_90d,benchmark_return_90d,market,outcome) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,'回填')",
        (date, ticker, rec, pc, r7, b7, r30, b30, r90, b90, market),
    )


# ── P0-4：多窗口 ───────────────────────────────────────────────
def test_alpha_by_window_consumes_30_90(tmp_path):
    db = tmp_path / "t.db"
    tracker.init_db(db)
    with tracker._conn(db) as con:
        # bullish：7天 alpha=+4(5-1)、30天 alpha=+8(12-4)、90天未回填(NULL)
        _insert_rec(con, "2026-05-01", "NVDA", "买入", "小加1",
                    5.0, 1.0, 12.0, 4.0, None, None)
    m = compute_value_metrics(db)
    abw = m["alpha_by_window"]
    assert abw["7d"]["alpha_avg"] == 4.0
    assert abw["30d"]["alpha_avg"] == 8.0
    assert abw["90d"] is None          # 90天无样本 → None（优雅"未到"，不崩）


def test_score_window_bearish_sign_and_neutral_band():
    # bearish 反号：卖出后续跌(ret<0)=帮你躲跌=正贡献
    rows = [{"recommendation": "卖出", "position_change": "", "return_30d": -10.0,
             "benchmark_return_30d": -2.0}]
    s = _score_window(rows, "return_30d", "benchmark_return_30d", 3.0)
    assert s["alpha_avg"] == 8.0       # -(-10 - -2) = +8
    assert s["win_rate"] == 100.0      # 卖出后跌=对
    # 30天中性带=3：|ret|=2<3 → 不计命中
    rows2 = [{"recommendation": "买入", "position_change": "", "return_30d": 2.0,
              "benchmark_return_30d": 0.0}]
    s2 = _score_window(rows2, "return_30d", "benchmark_return_30d", 3.0)
    assert s2["n_judged"] == 0         # 落在中性带，不判对错


# ── P0-3：verdict 止血——主语切回"信号"，不冒充"你账户截至本期" ──
def _seed_gate_passing_negative_alpha(db):
    """32 条减仓建议、8 票，卖出后均上涨(卖飞)→组合 alpha 为负、闸门通过。"""
    tracker.init_db(db)
    with tracker._conn(db) as con:
        for i in range(32):
            _insert_rec(con, f"2026-05-{(i % 27) + 1:02d}", f"TK{i % 8}",
                        "减仓", "减仓1", 5.0, 1.0)   # bearish, ret>0 → 卖飞；eff=-(5-1)=-4


def test_verdict_subject_is_signal_not_account(tmp_path):
    db = tmp_path / "t.db"
    _seed_gate_passing_negative_alpha(db)
    m = compute_value_metrics(db)
    assert m["gate"]["gate_passed"] is True      # n=32≥30, 8票, bm 100%
    txt = m["verdict"]["text"]
    # 止血：必须点明是"信号"的"第7天"表现，且明确不是账户累计
    assert "信号" in txt
    assert "第7天" in txt
    assert "不是你账户" in txt or "非你账户" in txt
    # 不得再用这些把信号超额冒充成账户截至今天的结论
    assert "截至本期" not in txt
    assert "不如躺平买指数" not in txt

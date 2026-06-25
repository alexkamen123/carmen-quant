# tests/test_backtest/test_advice_backtest.py
"""回测裁判：对历史减仓建议回测一条卖出守门规则的判别力（precision vs 卖飞基率），
带反过拟合的样本充分性闸门——样本不够/单一行情，即使 precision 高也不判「validated」、
不许据此自动改核心建议（防牛市过拟合）。sig_provider 注入，测试不依赖网络。"""
from types import SimpleNamespace
from finance_agent.backtest.advice_backtest import (
    backtest_sell_rule, backtest_sell_guard, load_sell_recs,
    MIN_EVAL_N, MIN_FLAGGED,
)
from finance_agent.db import tracker


def _rec(ret, bench):
    return {"ticker": "X", "date": "2026-05-01", "recommendation": "减仓",
            "return_7d": ret, "benchmark_return_7d": bench}


def test_confusion_and_precision():
    # 4 条：2 flag且卖飞(TP)、1 flag但卖对(FP)、1 没flag但卖飞(FN)
    recs = [_rec(10, 0), _rec(8, 0), _rec(-5, 0), _rec(12, 0)]
    flags = [True, True, True, False]
    it = iter(flags)
    r = backtest_sell_rule(recs, rule_fn=lambda rec, sig: next(it),
                           sig_provider=lambda rec: object())
    assert r["tp"] == 2 and r["fp"] == 1 and r["fn"] == 1 and r["tn"] == 0
    assert abs(r["precision"] - 2/3) < 0.01           # FLAG 中真卖飞 2/3
    assert abs(r["base_rate"] - 3/4) < 0.01           # 全样本卖飞 3/4


def test_skip_when_sig_unavailable():
    recs = [_rec(10, 0), _rec(-5, 0)]
    r = backtest_sell_rule(recs, rule_fn=lambda rec, sig: True,
                           sig_provider=lambda rec: None)   # 取数失败全跳过
    assert r["n_skip"] == 2 and r["n_eval"] == 0


def test_verdict_insufficient_small_sample():
    """样本 < 闸门 → insufficient，即便规则看着准也不许 validated（防过拟合）。"""
    recs = [_rec(10, 0)] * 5
    r = backtest_sell_rule(recs, rule_fn=lambda rec, sig: True,
                           sig_provider=lambda rec: object())
    assert r["n_eval"] < MIN_EVAL_N
    assert r["verdict"] == "insufficient_sample"


def test_verdict_validated_when_sufficient_and_discriminating():
    # 40 条：前 20 条 flag 且全卖飞(precision 高)，后 20 条不 flag 且卖对
    recs = [_rec(10, 0) for _ in range(20)] + [_rec(-10, 0) for _ in range(20)]
    rule = lambda rec, sig: rec["return_7d"] > 0
    r = backtest_sell_rule(recs, rule_fn=rule, sig_provider=lambda rec: object())
    assert r["n_eval"] >= MIN_EVAL_N and r["tp"] + r["fp"] >= MIN_FLAGGED
    assert r["precision"] > r["base_rate"]
    assert r["discriminating"] is True and r["sufficient"] is True
    assert r["verdict"] == "validated"


def test_verdict_no_discrimination():
    # 足量样本但规则瞎 flag（precision≈基率）→ no_discrimination，该 revert
    recs = [_rec(10, 0) for _ in range(20)] + [_rec(-10, 0) for _ in range(20)]
    rule = lambda rec, sig: True            # 全 flag → precision==base_rate
    r = backtest_sell_rule(recs, rule_fn=rule, sig_provider=lambda rec: object())
    assert r["discriminating"] is False
    assert r["verdict"] == "no_discrimination"


def _ins_sell(con, date, ticker, ret7, bm7):
    con.execute(
        "INSERT INTO recommendations(date,ticker,recommendation,position_change,"
        "price_at_rec,market,return_7d,benchmark_return_7d,outcome) VALUES(?,?,?,?,?,?,?,?,?)",
        (date, ticker, "减仓", "", 100.0, "us", ret7, bm7, "回填"))


def test_load_sell_recs_filters(tmp_path):
    db = tmp_path / "t.db"
    tracker.init_db(db)
    with tracker._conn(db) as con:
        _ins_sell(con, "2026-05-01", "MU", 10.0, 1.0)
        # 买入不取
        con.execute("INSERT INTO recommendations(date,ticker,recommendation,position_change,"
                    "price_at_rec,market,return_7d,benchmark_return_7d,outcome) "
                    "VALUES('2026-05-02','AAA','买入','',100,'us',5,1,'回填')")
    recs = load_sell_recs(db)
    assert len(recs) == 1 and recs[0]["ticker"] == "MU"


def test_backtest_sell_guard_with_injected_sig(tmp_path):
    """实跑包装用注入 sig（不碰网络）：强势 sig → flag_sell_into_strength 命中。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    with tracker._conn(db) as con:
        _ins_sell(con, "2026-05-01", "MU", 20.0, 2.0)     # 卖飞(超额>0)
    strong = SimpleNamespace(rsi=60, close=110, ma20=100, ma60=90, macd=1.2)  # 强势上行
    r = backtest_sell_guard(db, sig_provider=lambda rec: strong)
    assert r["n_eval"] == 1 and r["tp"] == 1               # flag 且真卖飞
    assert r["verdict"] == "insufficient_sample"           # 1 条远不够，不许 validated

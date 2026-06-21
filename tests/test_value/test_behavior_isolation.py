# tests/test_value/test_behavior_isolation.py
"""阶段2 三层隔离：对冲货基(SGOV 等现金等价物)绝不混入「主动选股」战绩。

病灶：active_buys 无过滤，SGOV(+0.15%≈现金)被算进「你主动选股」，把纯个股选股收益
稀释、还在逐笔列表标 🟢赚 与 NVDA 并列 → 让用户高估选股能力(反向撒谎)。
修复：复用 weekly.drift.HEDGE_TICKERS 单一真相源，对冲标的标 kind=cash、单列、不计选股胜负。
"""
from finance_agent.db import tracker
from finance_agent.value.metrics import compute_value_metrics
from finance_agent.value.report import _behavior_section, _adv_section
from finance_agent.weekly.drift import HEDGE_TICKERS


def _seed_actions(con, rows):
    for d, t, a, ret in rows:
        con.execute(
            "INSERT INTO user_actions(date,ticker,action,actual_return,source) "
            "VALUES(?,?,?,?,'manual')", (d, t, a, ret),
        )


def test_sgov_is_a_recognized_hedge():
    """前提：SGOV 在共享白名单里，不另起炉灶。"""
    assert "SGOV" in HEDGE_TICKERS


def test_hedge_excluded_from_active_selection(tmp_path):
    db = tmp_path / "t.db"
    tracker.init_db(db)
    with tracker._conn(db) as con:
        _seed_actions(con, [("2026-05-20", "NVDA", "BUY", 5.0),    # 真个股选股
                            ("2026-06-09", "SGOV", "BUY", 0.15)])  # 现金对冲，不算选股
    avp = compute_value_metrics(db)["active_vs_passive"]
    assert avp["active_n"] == 1                       # 只数 NVDA
    assert abs(avp["active_avg"] - 5.0) < 1e-6        # 不被 SGOV +0.15 稀释


def test_behavior_marks_hedge_as_cash(tmp_path):
    db = tmp_path / "t.db"
    tracker.init_db(db)
    with tracker._conn(db) as con:
        _seed_actions(con, [("2026-05-20", "NVDA", "BUY", 5.0),
                            ("2026-06-09", "SGOV", "BUY", 0.15)])
    trades = compute_value_metrics(db)["behavior"]["trades"]
    kinds = {t["ticker"]: t["kind"] for t in trades}
    assert kinds["NVDA"] == "stock"
    assert kinds["SGOV"] == "cash"


def test_behavior_section_marks_cash_not_green(tmp_path):
    """渲染层：对冲单列💵、不混入🟢选股赚；个股统计只数个股笔数。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    with tracker._conn(db) as con:
        _seed_actions(con, [("2026-05-20", "NVDA", "BUY", 5.0),
                            ("2026-06-09", "SGOV", "BUY", 0.15)])
    txt = _behavior_section(compute_value_metrics(db))
    assert "💵" in txt                                        # 现金单列标识
    sgov_line = next(l for l in txt.split("\n") if "SGOV" in l)
    assert "🟢" not in sgov_line                              # 对冲不冒充选股赚
    assert "1 笔个股操作" in txt                               # 统计只数个股(NVDA)


def test_active_buy_line_disambiguated_from_advice(tmp_path):
    """「你主动买入」(你实操) 必须和「让你买的」(我们建议、算超额) 点清区别，防两个买入数字反向打架。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    with tracker._conn(db) as con:
        _seed_actions(con, [("2026-05-20", "NVDA", "BUY", 5.0)])
    txt = _adv_section(compute_value_metrics(db))
    assert "你主动买入" in txt
    assert "不是一回事" in txt                                 # 消歧注，避免与「让你买的」混

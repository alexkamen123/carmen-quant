# tests/test_value/test_composition.py
"""阶段0 止血回归：composition 必须守恒、且 passive 是真实「持有/定投/观望」条数。

旧 BUG（267/252 口径污染）：neutral_or_passive = filled(未去重) - n_dir(去重后)，
把同票同周去重砍掉的方向性信号谎报成「持有/定投」，对外撒谎且自砸「诚实记分牌」招牌。
修复后 composition 把 filled 切成三个互斥且并集完整的子集，各自 COUNT，守恒不变式守门。
"""
from finance_agent.db import tracker
from finance_agent.value.metrics import compute_value_metrics
from finance_agent.value.report import _meta_section


def _insert(con, date, ticker, rec, pc, ret, bm, is_watch=0):
    con.execute(
        "INSERT INTO recommendations(date,ticker,recommendation,position_change,"
        "return_7d,benchmark_return_7d,market,is_watch,outcome) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (date, ticker, rec, pc, ret, bm, "us", is_watch,
         None if ret is None else "回填"),
    )


def _seed(con):
    """构造已知分布：
    passive=8（持有00700×5同周 + 定投QQQM×2 + 观望PLTR×1）
    directional_raw=4（减仓NVDA×3同周 + 买入FUTU×1）→ 去重后 directional=2、dedup_dropped=2
    shadow=2（影子买入GS×2，is_watch=1）
    filled=14
    """
    for d in ("2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05"):
        _insert(con, d, "00700", "持有", "维持", 1.5, 0.5)          # passive 5
    _insert(con, "2026-06-01", "QQQM", "按计划定投", "", 0.8, 0.6)   # passive
    _insert(con, "2026-06-08", "QQQM", "按计划定投", "", 0.4, 0.3)   # passive
    _insert(con, "2026-06-02", "PLTR", "观望", "维持", -0.5, 0.2)    # passive
    for d in ("2026-06-01", "2026-06-02", "2026-06-03"):
        _insert(con, d, "NVDA", "减仓", "减仓", -2.0, 1.0)          # dir_raw 3 → dedup 1
    _insert(con, "2026-06-05", "FUTU", "买入", "维持", 5.0, 1.0)     # dir_raw 1
    _insert(con, "2026-06-01", "GS", "买入", "维持", 3.0, 1.0, is_watch=1)  # shadow
    _insert(con, "2026-06-02", "GS", "买入", "维持", 2.0, 1.0, is_watch=1)  # shadow


def test_composition_conservation(tmp_path):
    """三子集守恒：directional + dedup_dropped + passive + shadow == filled。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    with tracker._conn(db) as con:
        _seed(con)
    comp = compute_value_metrics(db)["composition"]
    assert (comp["directional"] + comp["dedup_dropped"]
            + comp["passive"] + comp["shadow"]) == comp["filled"]


def test_passive_is_true_count_not_subtraction(tmp_path):
    """passive 必须是真实「持有/定投/观望」条数(8)，而非旧 BUG 的 filled-n_dir(=12)。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    with tracker._conn(db) as con:
        _seed(con)
    comp = compute_value_metrics(db)["composition"]
    assert comp["filled"] == 14
    assert comp["passive"] == 8           # 旧 BUG 会算成 14-2=12，对用户撒谎
    assert comp["directional_raw"] == 4
    assert comp["directional"] == 2       # 同周去重后
    assert comp["dedup_dropped"] == 2     # 被去重砍掉、单列透明交代
    assert comp["shadow"] == 2
    # 旧字段名彻底移除，杜绝下游误用减法口径
    assert "neutral_or_passive" not in comp


def test_meta_section_honest_numbers(tmp_path):
    """诚实附录数据构成行：数字必须和 composition 对得上、不得出现旧减法谎报值。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    with tracker._conn(db) as con:
        _seed(con)
    m = compute_value_metrics(db)
    txt = _meta_section(m)        # 旧代码引用 neutral_or_passive 会在此 KeyError
    assert "8 条" in txt          # passive 真值（持有/定投/观望）
    assert "12" not in txt        # 旧 BUG 的 filled-n_dir=12 谎报值绝不出现
    assert "选股名单" in txt        # shadow（2 条）有交代


def test_actionable_ratio_same_denominator(tmp_path):
    """actionable_ratio 分子分母同口径：去重方向 / (去重方向 + passive)，不混入影子。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    with tracker._conn(db) as con:
        _seed(con)
    comp = compute_value_metrics(db)["composition"]
    assert abs(comp["actionable_ratio"] - 2 / (2 + 8)) < 1e-6

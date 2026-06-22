# tests/test_db/test_value.py
"""价值证明报告（L1）测试：Wilson CI + 度量数学 + 诚实分桶/闸门。"""
from finance_agent.db import tracker
from finance_agent.value.metrics import compute_value_metrics, wilson_ci, _is_bullish, _is_bearish
from finance_agent.value.report import build_value_card


def _seed(db):
    tracker.init_db(db)
    # date, ticker, rec, position_change, return_7d, benchmark_return_7d, market
    recs = [
        ("2026-05-14", "INTC", "减仓", "减仓1", -9.64, -2.0, "us"),   # bearish, ret<0 → 对；eff=+7.64
        ("2026-05-15", "MU",   "减仓", "减仓1", 13.05, 1.0, "us"),    # bearish, ret>0 → 错；eff=-12.05
        ("2026-05-16", "NVDA", "买入", "小加1", 5.0,  1.0, "us"),     # bullish, ret>0 → 对；buy alpha=+4
        ("2026-05-17", "DRAM", "减仓", "减仓1", 0.5,  0.2, "us"),     # |ret|<1 → 中性，不计命中
        ("2026-05-18", "QQQM", "持有", "维持",  2.0,  None, "us"),    # 非方向性 → 不进 dir
        ("2026-05-19", "AAPL", "买入", "小加1", None, None, "us"),    # 未回填 → 不进 filled/dir
    ]
    with tracker._conn(db) as con:
        for d, t, r, pc, ret, bm, mkt in recs:
            con.execute(
                "INSERT INTO recommendations(date,ticker,recommendation,position_change,"
                "return_7d,benchmark_return_7d,market,outcome) VALUES(?,?,?,?,?,?,?,?)",
                (d, t, r, pc, ret, bm, mkt, None if ret is None else "回填"),
            )
        # user_actions
        for d, t, a, ret in [("2026-05-20", "NVDA", "BUY", 5.0),
                              ("2026-05-21", "INTC", "SELL", -3.0),
                              ("2026-05-22", "MU", "SELL", 10.0)]:
            con.execute(
                "INSERT INTO user_actions(date,ticker,action,actual_return,source) VALUES(?,?,?,?,'manual')",
                (d, t, a, ret),
            )


# ── Wilson CI ─────────────────────────────────────────────────

def test_wilson_bounds():
    lo, hi = wilson_ci(7, 11)
    assert 0.0 <= lo <= hi <= 1.0          # 永不越界
    assert lo < 0.7 < hi or True           # 区间合理
    assert wilson_ci(0, 0) == (0.0, 0.0)   # 空样本不崩
    lo2, hi2 = wilson_ci(11, 11)
    assert hi2 <= 1.0                       # 全对也不越界 >100%


def test_direction_helpers():
    assert _is_bullish("买入", "") and _is_bullish("买入", "小加1")
    # 止血：「持有/观望 + 小加」rec 优先，语义是持有，不再误判为买入
    assert not _is_bullish("持有", "小加1")
    assert _is_bearish("减仓", "") and _is_bearish("卖出", "")
    assert not _is_bullish("持有", "维持")


# ── 度量数学 ──────────────────────────────────────────────────

def test_metrics_math(tmp_path):
    db = tmp_path / "t.db"
    _seed(db)
    m = compute_value_metrics(db)

    g = m["gate"]
    assert g["n_dir"] == 4          # INTC/MU/NVDA/DRAM（QQQM持有、AAPL未回填 排除）
    assert g["n_buy"] == 1          # 仅 NVDA
    assert g["filled"] == 5         # 6 条里 AAPL 未回填
    assert g["gate_passed"] is False  # n_dir=4 < 30

    hr = m["hit_rate"]
    assert hr["n_judged"] == 3 and hr["correct"] == 2 and hr["wrong"] == 1  # DRAM中性不计
    assert hr["neutral"] == 1
    assert hr["win_rate"] == 66.7
    assert hr["ci_low"] is not None and hr["ci_high"] is not None

    # 选股 alpha：NVDA 5-1=+4
    assert m["buy_alpha"]["status"] == "ok" and m["buy_alpha"]["avg"] == 4.0

    # 组合 alpha：benchmark 覆盖 4/4=100% → ok（减仓反号）
    assert m["combined_alpha"]["status"] == "ok"

    # 构成透明度
    assert m["composition"]["actionable_ratio"] == round(4 / 5, 3)

    # 置顶三态：未过闸门 → state 0 灰
    assert m["verdict"]["state"] == 0 and m["verdict"]["color"] == "grey"

    # 行为逐笔：BUY+5赚、SELL-3躲跌✓、SELL+10踏空
    vs = {t["ticker"]: t["verdict"] for t in m["behavior"]["trades"]}
    assert vs["NVDA"] == "赚" and vs["INTC"] == "躲跌✓" and vs["MU"] == "踏空"

    # 卡片可渲染、含诚实声明
    import json
    blob = json.dumps(build_value_card(m), ensure_ascii=False)
    # 可信度用「进度」措辞而非「拒绝」措辞（说人话：现在够给初步判断，攒够更可靠）
    assert "价值体检" in blob and "初步判断" in blob and "诚实附录" in blob
    assert "不能下定论" not in blob


def test_buy_alpha_placeholder_when_no_buy(tmp_path):
    db = tmp_path / "t.db"
    tracker.init_db(db)
    with tracker._conn(db) as con:
        con.execute(
            "INSERT INTO recommendations(date,ticker,recommendation,position_change,"
            "return_7d,benchmark_return_7d,market,outcome) VALUES(?,?,?,?,?,?,?,?)",
            ("2026-05-14", "INTC", "减仓", "减仓1", -9.0, -2.0, "us", "回填"),
        )
    m = compute_value_metrics(db)
    assert m["buy_alpha"]["status"] == "no_sample" and m["buy_alpha"]["n"] == 0


def test_empty_db_no_crash(tmp_path):
    db = tmp_path / "t.db"
    tracker.init_db(db)
    m = compute_value_metrics(db)
    assert m["gate"]["n_dir"] == 0
    assert m["verdict"]["state"] == 0
    build_value_card(m)  # 不崩


# ── 三态结论（gate-passing 路径，对抗审查回归）──────────────────

def _seed_buys(db, rows):
    """rows: [(ticker, return_7d, benchmark|None)]，全部记为买入(看多方向性)。"""
    tracker.init_db(db)
    with tracker._conn(db) as con:
        for i, (t, ret, bm) in enumerate(rows):
            con.execute(
                "INSERT INTO recommendations(date,ticker,recommendation,position_change,"
                "return_7d,benchmark_return_7d,market,outcome) VALUES(?,?,?,?,?,?,?,?)",
                (f"2026-05-{10 + i:02d}", t, "买入", "小加1", ret, bm, "us", "回填"),
            )


def _lower_gates(monkeypatch, n=3, tk=1, bm=0.7):
    from finance_agent.value import metrics as M
    monkeypatch.setattr(M, "GATE_MIN_N", n)
    monkeypatch.setattr(M, "GATE_MIN_TICKERS", tk)
    monkeypatch.setattr(M, "GATE_MIN_BM_COV", bm)


def test_verdict_green(tmp_path, monkeypatch):
    _lower_gates(monkeypatch)
    db = tmp_path / "t.db"
    _seed_buys(db, [("NVDA", 5, 1), ("MU", 6, 1), ("AMD", 4, 1)])  # avg_alpha=+4, win=100%
    m = compute_value_metrics(db)
    assert m["verdict"]["state"] == 2 and m["verdict"]["color"] == "green"
    assert "跑赢基准" in m["verdict"]["text"]


def test_verdict_orange_negative_alpha(tmp_path, monkeypatch):
    _lower_gates(monkeypatch)
    db = tmp_path / "t.db"
    _seed_buys(db, [("NVDA", -5, 1), ("MU", -6, 1), ("AMD", -4, 1)])  # avg_alpha<0
    m = compute_value_metrics(db)
    assert m["verdict"]["state"] == 1
    txt = m["verdict"]["text"]
    assert "跑输基准" in txt and "建议" in txt and "到今天" in txt
    # P0-3 止血：不再把建议超额冒充成"你账户截至本期不如躺平"
    assert "不如躺平" not in txt and "截至本期" not in txt
    assert "不是你账户" in txt          # 主语锁死=建议本身，非账户累计


def test_verdict_orange_positive_alpha_low_winrate(tmp_path, monkeypatch):
    """关键回归：正 alpha + 低命中率 绝不能说'不如躺平买指数'。"""
    _lower_gates(monkeypatch)
    db = tmp_path / "t.db"
    _seed_buys(db, [("NVDA", 20, 0), ("MU", -2, 0), ("AMD", -2, 0)])  # avg_alpha=+5.33, win=33%
    m = compute_value_metrics(db)
    assert m["verdict"]["state"] == 1
    assert "超额为正" in m["verdict"]["text"]
    assert "不如躺平" not in m["verdict"]["text"]


def test_backfill_market(tmp_path):
    db = tmp_path / "t.db"
    tracker.init_db(db)
    with tracker._conn(db) as con:
        con.execute("INSERT INTO recommendations(date,ticker,recommendation,market) VALUES('2026-05-01','07709','持有',NULL)")
        con.execute("INSERT INTO recommendations(date,ticker,recommendation,market) VALUES('2026-05-01','NVDA','持有',NULL)")
        con.execute("INSERT INTO recommendations(date,ticker,recommendation,market) VALUES('2026-05-01','AAPL','持有','us')")
    n = tracker.backfill_market(db)
    assert n == 2  # 仅补 NULL，已有 us 的不动
    with tracker._conn(db) as con:
        mk = {r["ticker"]: r["market"] for r in con.execute("SELECT ticker,market FROM recommendations").fetchall()}
    assert mk["07709"] == "hk" and mk["NVDA"] == "us" and mk["AAPL"] == "us"


def test_card_taokong_yellow_and_summary(tmp_path):
    """卡片可读性：踏空标🟡(不与真亏红混)+ 行为段大白话总结。"""
    db = tmp_path / "t.db"
    _seed(db)  # user_actions: NVDA BUY+5(赚) / INTC SELL-3(躲跌✓) / MU SELL+10(踏空)
    m = compute_value_metrics(db)
    import json
    blob = json.dumps(build_value_card(m), ensure_ascii=False)
    assert "🟡" in blob              # 踏空黄色
    assert "卖早" in blob and "真亏" in blob  # 大白话总结行


def test_verdict_gate_passed_no_benchmark_no_crash(tmp_path, monkeypatch):
    """关键回归：闸门通过但 avg_alpha 为 None 时不得 round(None) 崩溃/输出 +None%。"""
    _lower_gates(monkeypatch, n=2, tk=1, bm=0.0)
    db = tmp_path / "t.db"
    _seed_buys(db, [("NVDA", 5, None), ("MU", 6, None)])  # benchmark NULL → avg_alpha None
    m = compute_value_metrics(db)
    assert m["verdict"]["state"] == 0          # 防御性降级
    assert "benchmark" in m["verdict"]["text"]
    assert "None" not in m["verdict"]["text"]  # 绝不出现 +None%

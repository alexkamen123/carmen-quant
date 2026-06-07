# tests/test_db/test_scorecard.py
"""月度行为打分 + 逐笔反事实复盘（P3）测试。"""
from finance_agent.db import tracker
from finance_agent.db.tracker import get_period_actions, guidance_month_summary, save_guidance
from finance_agent.monthly.scorecard import (
    _grade_trade, build_trade_review, trade_review_summary, _parse_scorecard,
)


# ── 逐笔评级（规则化反事实）──────────────────────────────────

def test_grade_buy():
    assert _grade_trade("BUY", 5.0)[0] == "✅"
    assert _grade_trade("BUY", -8.0)[0] == "❌"
    assert _grade_trade("BUY", 0.5)[0] == "⚠️"


def test_grade_sell():
    # SELL 远期为负 = 躲过下跌 = 卖对了
    assert _grade_trade("SELL", -9.0)[0] == "✅"
    # SELL 远期为正 = 卖早了
    assert _grade_trade("SELL", 20.0)[0] == "❌"
    assert _grade_trade("TRIM", 0.0)[0] == "⚠️"


def test_grade_counterfactual_text():
    assert "躲过 9.0%" in _grade_trade("SELL", -9.0)[1]
    assert "多赚 +20.0%" in _grade_trade("SELL", 20.0)[1]
    assert "省" in _grade_trade("BUY", -12.0)[1]


# ── 期间查询 ──────────────────────────────────────────────────

def _seed_actions(db):
    tracker.init_db(db)
    rows = [
        ("2026-05-11", "MU",   "BUY",  1, 780.0, -10.4),
        ("2026-05-21", "DRAM", "BUY",  2, 48.96, 29.1),
        ("2026-05-21", "07709", "SELL", 20, 94.52, 52.7),
        ("2026-06-02", "AAPL", "BUY",  1, 308.93, None),  # 未回填，应被排除
    ]
    with tracker._conn(db) as con:
        for d, t, a, s, pr, ret in rows:
            con.execute(
                "INSERT INTO user_actions(date,ticker,action,shares,price,actual_return,source) "
                "VALUES(?,?,?,?,?,?,'manual')",
                (d, t, a, s, pr, ret),
            )


def test_get_period_actions_filters(tmp_path):
    db = tmp_path / "t.db"
    _seed_actions(db)
    # 5 月区间：3 条有 actual_return（AAPL 在 6 月且 None，排除）
    rows = get_period_actions("2026-05-01", "2026-05-31", db_path=db)
    assert len(rows) == 3
    assert all(r["actual_return"] is not None for r in rows)


def test_build_trade_review_grades(tmp_path):
    db = tmp_path / "t.db"
    _seed_actions(db)
    reviewed = build_trade_review("2026-05-01", "2026-05-31", db_path=str(db))
    by = {r["ticker"]: r for r in reviewed}
    assert by["DRAM"]["grade"] == "✅"     # BUY +29% 赚
    assert by["MU"]["grade"] == "❌"        # BUY -10% 亏
    assert by["07709"]["grade"] == "❌"     # SELL 卖后 +52% 卖早了
    # 复盘摘要可生成
    assert "DRAM" in trade_review_summary(reviewed)


# ── 指导期间计数 ──────────────────────────────────────────────

def test_guidance_month_summary(tmp_path):
    db = tmp_path / "t.db"
    save_guidance([{"ticker": "IAU,SGOV", "action": "建仓", "target": "x"}],
                  source="weekly", db_path=db)
    s = guidance_month_summary("2000-01-01", "2099-12-31", db_path=db)
    assert s["open"] == 1 and s["followed"] == 0 and s["expired"] == 0


# ── 打分 JSON 解析鲁棒性 ──────────────────────────────────────

def test_parse_scorecard_valid():
    raw = '{"dimensions":[{"name":"执行纪律","score":4,"reason":"x"}],"overall":"y"}'
    d = _parse_scorecard(raw)
    assert d["dimensions"][0]["score"] == 4 and d["overall"] == "y"


def test_parse_scorecard_clamps_and_markdown():
    raw = '```json\n{"dimensions":[{"name":"a","score":15},{"name":"b","score":"bad"}]}\n```'
    d = _parse_scorecard(raw)
    assert d["dimensions"][0]["score"] == 10   # 15 钳到 10
    assert d["dimensions"][1]["score"] == 5    # 非法 → 5


def test_parse_scorecard_fallback_on_garbage():
    d = _parse_scorecard("这不是JSON")
    assert len(d["dimensions"]) == 5  # 兜底 5 维


def test_parse_scorecard_dimensions_not_list():
    # dimensions 是非空字符串（非 list）→ 兜底，不崩
    d = _parse_scorecard('{"dimensions":"bad","overall":"x"}')
    assert len(d["dimensions"]) == 5


def test_parse_scorecard_skips_nondict_elements():
    # list 里混入非 dict 元素 → 跳过畸形，不崩
    d = _parse_scorecard('{"dimensions":[1,2,{"name":"执行纪律","score":4}]}')
    assert len(d["dimensions"]) == 1 and d["dimensions"][0]["score"] == 4


def test_parse_scorecard_fills_missing_name():
    # dimension 缺 name → 补默认，避免下游 KeyError
    d = _parse_scorecard('{"dimensions":[{"score":4,"reason":"x"}]}')
    assert d["dimensions"][0]["name"] == "未知维度"


def test_build_trade_review_tolerates_none_shares(tmp_path):
    # shares 为 NULL 的操作（log-action 省略 --shares）不应让复盘崩溃
    db = tmp_path / "t.db"
    tracker.init_db(db)
    with tracker._conn(db) as con:
        con.execute(
            "INSERT INTO user_actions(date,ticker,action,shares,price,actual_return,source) "
            "VALUES('2026-05-15','NVDA','BUY',NULL,NULL,5.0,'manual')",
        )
    reviewed = build_trade_review("2026-05-01", "2026-05-31", db_path=str(db))
    assert len(reviewed) == 1 and reviewed[0]["shares"] is None
    assert reviewed[0]["grade"] == "✅"

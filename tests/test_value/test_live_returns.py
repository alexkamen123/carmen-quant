# tests/test_value/test_live_returns.py
"""逐笔操作「到今天」的真实涨跌(动态实时拉价)——回答用户质疑：
第 7 天定格不合理(7 天涨 30 天亏怎么办)，应看最新。

口径：用与 7 日定格同源的 entry price + 今日价，尺度一致、可并排显示。
拉价失败的笔不计入(渲染层 fallback 7 日定格)。
"""
from finance_agent.db import tracker
from finance_agent.value.cumulative import compute_live_action_returns
from finance_agent.value.report import _behavior_section


def _trade(**kw):
    base = {"id": 1, "date": "2026-05-11", "ticker": "MU", "action": "BUY",
            "ret": -10.42, "verdict": "亏", "kind": "stock", "live_return": None}
    base.update(kw)
    return base


def _beh(trades):
    return {"behavior": {"n": len(trades), "trades": trades, "pending": [],
                         "symbol_note": "卖出口径说明"}}


def test_behavior_prefers_live_over_7d():
    """7 日定格 -10.4%(亏) 但至今 +45.4%(赚)——主显示至今、重判为赚，7 日作参考。"""
    txt = _behavior_section(_beh([_trade(live_return=45.4)]))
    assert "至今 +45.4%" in txt
    assert "· 赚" in txt              # 至今为正 → 赚（覆盖 7 日的"亏"）
    assert "🟢" in txt                # 赚 = 绿
    assert "10.42" in txt             # 7 日定格仍作参考保留
    assert "真亏 **0**" in txt         # 统计基于至今：不再算成真亏


def test_behavior_fallback_to_7d_when_no_live():
    """拉价失败(live=None) → 退显第 7 天定格、按 7 日判，并标注。"""
    txt = _behavior_section(_beh([_trade(live_return=None)]))
    assert "🔴" in txt                # 7 日 -10.4% = 亏 = 红
    assert "第7天" in txt              # fallback 标注


def _ins(con, date, ticker, action, price):
    con.execute(
        "INSERT INTO user_actions(date,ticker,action,price,source) VALUES(?,?,?,?,'manual')",
        (date, ticker, action, price),
    )


def test_live_return_entry_to_today(tmp_path):
    db = tmp_path / "t.db"
    tracker.init_db(db)
    with tracker._conn(db) as con:
        _ins(con, "2026-05-11", "MU", "BUY", 780.0)      # 至今 (1134-780)/780 = +45.4
        _ins(con, "2026-05-29", "NVDA", "BUY", 216.13)   # 至今 (210.69-216.13)/216.13 = -2.5
    fake = lambda tk, mkt: {"MU": 1134.0, "NVDA": 210.69}.get(tk)
    out = compute_live_action_returns(db, price_fn=fake)
    assert len(out) == 2
    assert any(abs(v - 45.4) < 0.3 for v in out.values())
    assert any(abs(v - (-2.5)) < 0.3 for v in out.values())


def test_live_skips_when_price_unavailable(tmp_path):
    db = tmp_path / "t.db"
    tracker.init_db(db)
    with tracker._conn(db) as con:
        _ins(con, "2026-05-11", "MU", "BUY", 780.0)
    assert compute_live_action_returns(db, price_fn=lambda tk, mkt: None) == {}


def test_live_skips_when_no_entry_price(tmp_path):
    db = tmp_path / "t.db"
    tracker.init_db(db)
    with tracker._conn(db) as con:
        _ins(con, "2026-05-11", "MU", "BUY", None)       # 无 entry 无法算
    assert compute_live_action_returns(db, price_fn=lambda tk, mkt: 1000.0) == {}

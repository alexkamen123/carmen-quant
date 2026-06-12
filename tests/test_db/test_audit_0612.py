# tests/test_db/test_audit_0612.py
"""06-12 ultracode 自检修复回归（task#33）：口径分栏 / 双记 / disabled 同源。"""
from finance_agent.db import tracker


def _seed_rec(db, ticker, rec, ret, outcome, is_watch=0, pos=None, date=None):
    from datetime import datetime
    date = date or datetime.today().strftime("%Y-%m-%d")
    with tracker._conn(db) as con:
        con.execute(
            "INSERT INTO recommendations(date, ticker, recommendation, position_change, "
            "return_7d, outcome, market, is_watch) VALUES(?,?,?,?,?,?,'us',?)",
            (date, ticker, rec, pos, ret, outcome, is_watch),
        )


def test_accuracy_summary_excludes_shadow_and_splits_neutral(tmp_path):
    """影子票不进命中率；方向性中性带与持有/定投分开标注。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    _seed_rec(db, "MU", "买入", 5.0, "正确")
    _seed_rec(db, "TSM", "减仓", 2.0, "错误")
    _seed_rec(db, "NVDA", "买入", 0.5, "中性")          # 方向性落±1%中性带
    _seed_rec(db, "AAPL", "持有", -3.0, "中性")          # 真持有
    _seed_rec(db, "AVGO", "买入", 9.0, "正确", is_watch=1)   # 影子票，绝不进
    out = tracker.accuracy_summary(days=30, db_path=db)
    assert "命中率：50%" in out                  # 1对1错，AVGO 没混进来
    assert "±1%中性带1" in out                   # NVDA 单独标
    assert "另有 1 条持有/定投" in out            # 只有 AAPL


def test_weekly_summary_directional_denominator(tmp_path):
    """周报胜率分母只含方向性行；持有行单独计数不灌水。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    _seed_rec(db, "MU", "买入", 5.0, None)
    _seed_rec(db, "TSM", "卖出", 3.0, None)              # 卖后涨=错
    for i in range(8):
        _seed_rec(db, f"H{i}", "持有", 1.0, None)        # 8 条持有，不许进分母
    _seed_rec(db, "AVGO", "买入", 9.0, None, is_watch=1)  # 影子票排除
    ws = tracker.weekly_accuracy_summary(db_path=db)
    assert ws["total"] == 2 and ws["win_rate"] == 50      # 旧逻辑会是 9/10=90%
    assert ws["hold_n"] == 8 and ws["hold_ok"] == 8


def test_detect_skips_same_day_manual_record(tmp_path, monkeypatch):
    """写入侧防双记：当日已有手动 BUY → auto 检测跳过。"""
    import yaml
    db = tmp_path / "t.db"
    tracker.init_db(db)
    pf = tmp_path / "portfolio.yaml"
    pf.write_text(yaml.safe_dump({"holdings": [
        {"ticker": "AVGO", "market": "us", "shares": 1, "cost_basis": 378.9}]}))
    # 先建快照基线（空持仓）
    pf0 = tmp_path / "p0.yaml"
    pf0.write_text(yaml.safe_dump({"holdings": []}))
    tracker.detect_portfolio_changes(portfolio_path=pf0, db_path=db)
    # 用户手动记了这笔
    tracker.log_user_action(ticker="AVGO", action="BUY", shares=1, price=378.9, db_path=db)
    # auto 检测同一笔 → 必须跳过
    out = tracker.detect_portfolio_changes(portfolio_path=pf, db_path=db)
    assert out == []
    with tracker._conn(db) as con:
        n = con.execute("SELECT COUNT(*) FROM user_actions WHERE ticker='AVGO' "
                        "AND action='BUY'").fetchone()[0]
    assert n == 1                                          # 不许双记


def test_drop_disabled_both_conditions_same_source(monkeypatch):
    """disabled 回退：1h 与 open 两条件同源（threshold_pct），base_pct≠3 不分裂。"""
    import pandas as pd
    from types import SimpleNamespace
    from finance_agent.alerts import news_monitor as nm
    closes = [100.0] * 10 + [96.4, 96.4]   # 1h -3.6%
    monkeypatch.setattr(nm, "_is_market_open", lambda m: True)
    idx = pd.date_range("2026-06-12 09:30", periods=len(closes), freq="5min")
    df_5m = pd.DataFrame({"close": closes}, index=idx)
    df_2d = pd.DataFrame({"close": [100.0, 100.0]})
    df_d = pd.DataFrame({"close": [100.0] * 30})

    def dl(tkr, period=None, interval=None, **k):
        return df_5m if interval == "5m" else (df_2d if period == "2d" else df_d)

    monkeypatch.setattr(nm.yf, "download", dl)
    import finance_agent.signals.technical as tech
    monkeypatch.setattr(tech, "calculate_signals",
                        lambda df, ticker=None: SimpleNamespace(atr_pct=10.0, atr=1.0,
                                                                bb_lower=90, ma20=100, rsi=50))
    # 关 ATR 且 base_pct 改成 5.0：两条件都必须用 threshold_pct=3.0（与改动前一致）
    monkeypatch.setattr(nm, "_load_dip_atr_cfg",
                        lambda: {"enabled": False, "base_pct": 5.0, "k": 0.8, "cap_pct": 7.0})
    info = nm._check_price_drop("X", "us", 3.0)
    assert info is not None                                # -3.6% <= -3.0 触发
    assert info["effective_threshold"] == 3.0              # 不是 cfg 的 5.0

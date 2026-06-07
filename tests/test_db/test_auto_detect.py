# tests/test_db/test_auto_detect.py
"""持仓自动采集（detect_portfolio_changes）与 SELL 回填测试。"""
from datetime import datetime, timedelta

import pandas as pd
import pytest
import yaml

from finance_agent.db import tracker
from finance_agent.db.tracker import (
    detect_portfolio_changes,
    _infer_action_price,
    backfill_action_returns,
)


def _write_portfolio(path, holdings):
    path.write_text(yaml.safe_dump({"holdings": holdings}, allow_unicode=True))


# ── 边际买价反推 ──────────────────────────────────────────────

def test_infer_action_price():
    # 原 1 股 @312.38，加 1 股后均价 337.33 → 本次边际买价 = 362.28
    assert abs(_infer_action_price(2, 337.33, 1, 312.38) - 362.28) < 0.01
    # cost 缺失 → None
    assert _infer_action_price(2, None, 1, 312.38) is None
    # 非加仓（股数没增）→ None
    assert _infer_action_price(1, 300.0, 2, 300.0) is None


# ── 检测逻辑 ──────────────────────────────────────────────────

def test_first_run_baseline_only(tmp_path):
    pf, db = tmp_path / "portfolio.yaml", tmp_path / "t.db"
    _write_portfolio(pf, [{"ticker": "NVDA", "market": "us", "shares": 2, "cost_basis": 200}])
    # 首次：只建基线，0 操作
    assert detect_portfolio_changes(portfolio_path=pf, db_path=db) == []
    # 幂等：状态没变，第二次仍 0 操作
    assert detect_portfolio_changes(portfolio_path=pf, db_path=db) == []


def test_add_shares_infers_marginal_price(tmp_path):
    pf, db = tmp_path / "portfolio.yaml", tmp_path / "t.db"
    _write_portfolio(pf, [{"ticker": "GOOGL", "market": "us", "shares": 1, "cost_basis": 312.38}])
    detect_portfolio_changes(portfolio_path=pf, db_path=db)  # 基线
    _write_portfolio(pf, [{"ticker": "GOOGL", "market": "us", "shares": 2, "cost_basis": 337.33}])
    changes = detect_portfolio_changes(portfolio_path=pf, db_path=db)
    assert len(changes) == 1
    c = changes[0]
    assert c["action"] == "BUY" and c["shares"] == 1
    assert abs(c["price"] - 362.28) < 0.01  # 反推的边际买价


def test_new_ticker_build_position(tmp_path):
    pf, db = tmp_path / "portfolio.yaml", tmp_path / "t.db"
    _write_portfolio(pf, [{"ticker": "NVDA", "market": "us", "shares": 2, "cost_basis": 200}])
    detect_portfolio_changes(portfolio_path=pf, db_path=db)  # 基线
    _write_portfolio(pf, [
        {"ticker": "NVDA", "market": "us", "shares": 2, "cost_basis": 200},
        {"ticker": "AAPL", "market": "us", "shares": 1, "cost_basis": 308.93},
    ])
    changes = detect_portfolio_changes(portfolio_path=pf, db_path=db)
    assert len(changes) == 1
    c = changes[0]
    assert c["ticker"] == "AAPL" and c["action"] == "BUY"
    assert c["shares"] == 1 and c["price"] == 308.93


def test_reduce_shares_sell(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "_fetch_current_price", lambda t, m="us": 100.0)
    pf, db = tmp_path / "portfolio.yaml", tmp_path / "t.db"
    _write_portfolio(pf, [{"ticker": "07709", "market": "hk", "shares": 10, "cost_basis": 108}])
    detect_portfolio_changes(portfolio_path=pf, db_path=db)  # 基线
    _write_portfolio(pf, [{"ticker": "07709", "market": "hk", "shares": 8, "cost_basis": 108}])
    changes = detect_portfolio_changes(portfolio_path=pf, db_path=db)
    assert len(changes) == 1
    c = changes[0]
    assert c["ticker"] == "07709" and c["action"] == "SELL"
    assert c["shares"] == 2 and c["price"] == 100.0


def test_remove_ticker_liquidates(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "_fetch_current_price", lambda t, m="us": 50.0)
    pf, db = tmp_path / "portfolio.yaml", tmp_path / "t.db"
    _write_portfolio(pf, [
        {"ticker": "NVDA", "market": "us", "shares": 2, "cost_basis": 200},
        {"ticker": "QBTS", "market": "us", "shares": 2, "cost_basis": 28.81},
    ])
    detect_portfolio_changes(portfolio_path=pf, db_path=db)  # 基线
    _write_portfolio(pf, [{"ticker": "NVDA", "market": "us", "shares": 2, "cost_basis": 200}])
    changes = detect_portfolio_changes(portfolio_path=pf, db_path=db)
    assert len(changes) == 1
    c = changes[0]
    assert c["ticker"] == "QBTS" and c["action"] == "SELL" and c["shares"] == 2


def test_dry_run_does_not_persist(tmp_path):
    pf, db = tmp_path / "portfolio.yaml", tmp_path / "t.db"
    _write_portfolio(pf, [{"ticker": "NVDA", "market": "us", "shares": 1, "cost_basis": 200}])
    detect_portfolio_changes(portfolio_path=pf, db_path=db)  # 基线
    _write_portfolio(pf, [{"ticker": "NVDA", "market": "us", "shares": 2, "cost_basis": 220}])
    # dry-run 检测到变更但不写库、不更新快照
    dr = detect_portfolio_changes(portfolio_path=pf, db_path=db, dry_run=True)
    assert len(dr) == 1
    # 因为 dry-run 没更新快照，正式跑仍能检测到同一笔
    real = detect_portfolio_changes(portfolio_path=pf, db_path=db)
    assert len(real) == 1


# ── SELL 收益回填 ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sell_backfill_computes_return(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    tracker.init_db(db)
    old_date = (datetime.today() - timedelta(days=12)).strftime("%Y-%m-%d")
    with tracker._conn(db) as con:
        con.execute(
            "INSERT INTO user_actions(date,ticker,action,shares,price,source) "
            "VALUES(?,?,?,?,?,?)",
            (old_date, "TSM", "SELL", 1, 100.0, "auto"),
        )
    # mock 价格序列：第 7 个交易日(idx6)=90 → 卖出后跌了 10%（卖得好）
    dates = pd.date_range(old_date, periods=8)
    df = pd.DataFrame({"Close": [100, 98, 96, 95, 93, 92, 90, 89]}, index=dates)
    monkeypatch.setattr(tracker.yf, "download", lambda *a, **k: df)

    filled = await backfill_action_returns(db_path=db)
    assert filled == 1
    with tracker._conn(db) as con:
        row = con.execute(
            "SELECT actual_return FROM user_actions WHERE ticker='TSM'"
        ).fetchone()
    assert row["actual_return"] == -10.0  # 负=卖后下跌=卖对了

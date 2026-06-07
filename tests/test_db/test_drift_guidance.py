# tests/test_db/test_drift_guidance.py
"""三桶漂移 + 指导台账（P2）测试。"""
from datetime import datetime, timedelta

from finance_agent.db import tracker
from finance_agent.db.tracker import (
    save_guidance, check_guidance_adherence, get_open_guidance,
)
from finance_agent.weekly.drift import bucket_of, compute_drift, derive_guidance


_STRATEGY = {
    "monthly_dca": {"passive_beta_pct": 50, "hedge_pct": 25, "ammo_pct": 25},
    "hedge_bucket": {"target_pct": 12.0},
}


# ── 桶分类 ────────────────────────────────────────────────────

def test_bucket_of():
    assert bucket_of({"ticker": "QQQM", "sector": "宽基ETF", "is_dca": True}) == "passive"
    assert bucket_of({"ticker": "VOO", "sector": "宽基ETF", "is_dca": True}) == "passive"
    assert bucket_of({"ticker": "SCHD", "sector": "股息ETF"}) == "passive"
    assert bucket_of({"ticker": "IAU", "sector": "黄金"}) == "hedge"
    assert bucket_of({"ticker": "SGOV", "sector": "债券"}) == "hedge"
    assert bucket_of({"ticker": "NVDA", "sector": "半导体/AI算力"}) == "ammo"
    assert bucket_of({"ticker": "DRAM", "sector": "半导体/AI算力"}) == "ammo"


# ── 漂移计算 ──────────────────────────────────────────────────

def test_compute_drift():
    bucket_value = {"passive": 17.0, "hedge": 0.0, "ammo": 83.0}
    rows = compute_drift(bucket_value, total_value=100.0, strategy=_STRATEGY)
    by = {r["bucket"]: r for r in rows}
    assert by["passive"]["target_pct"] == 50 and by["passive"]["current_pct"] == 17.0
    assert by["passive"]["drift"] == -33.0 and by["passive"]["status"] == "🔴"
    assert by["hedge"]["current_pct"] == 0.0 and by["hedge"]["status"] == "🔴"
    assert by["ammo"]["drift"] == 58.0 and by["ammo"]["status"] == "🔴"


def test_compute_drift_within_band_is_ok():
    # 当前贴近目标（±5% 内）→ ✅
    rows = compute_drift({"passive": 48, "hedge": 24, "ammo": 28}, 100.0, _STRATEGY)
    assert all(r["status"] == "✅" for r in rows)


# ── 规则化指导生成 ────────────────────────────────────────────

def test_derive_guidance_triggers_all_three():
    drift_rows = compute_drift({"passive": 17, "hedge": 0, "ammo": 83}, 100.0, _STRATEGY)
    holding_value = {"NVDA": 20.0, "GOOGL": 10.0, "AAPL": 5.0}  # NVDA 20% 超 15% 红线
    items = derive_guidance(drift_rows, holding_value, total_value=100.0, strategy=_STRATEGY)
    by_action = {it["action"]: it for it in items}
    # 对冲桶空 → 建仓
    assert "建仓" in by_action and "IAU" in by_action["建仓"]["ticker"]
    # NVDA 超 15% → 减仓
    assert "减仓" in by_action and by_action["减仓"]["ticker"] == "NVDA"
    # 被动桶严重低配 → 补仓
    assert "补仓" in by_action and "QQQM" in by_action["补仓"]["ticker"]


def test_derive_guidance_no_breach():
    drift_rows = compute_drift({"passive": 50, "hedge": 25, "ammo": 25}, 100.0, _STRATEGY)
    holding_value = {"NVDA": 10.0, "GOOGL": 10.0}  # 无单票超 15%
    items = derive_guidance(drift_rows, holding_value, 100.0, _STRATEGY)
    assert items == []


# ── 指导台账：去重 + 执行检验 ─────────────────────────────────

def test_save_guidance_dedup(tmp_path):
    db = tmp_path / "t.db"
    items = [{"ticker": "IAU,SGOV", "action": "建仓", "target": "补对冲桶", "rationale": "x"}]
    assert save_guidance(items, source="weekly", db_path=db) == 1
    # 7 天内同 (source,ticker,action) 重复 → 跳过
    assert save_guidance(items, source="weekly", db_path=db) == 0
    assert len(get_open_guidance(db_path=db)) == 1


def test_check_adherence_followed(tmp_path):
    db = tmp_path / "t.db"
    save_guidance(
        [{"ticker": "IAU,SGOV", "action": "建仓", "target": "补对冲桶"}],
        source="weekly", db_path=db,
    )
    today = datetime.today().strftime("%Y-%m-%d")
    # 用户当天买入 IAU → 应判为已照做
    with tracker._conn(db) as con:
        con.execute(
            "INSERT INTO user_actions(date,ticker,action,shares,price,source) "
            "VALUES(?,?,?,?,?,?)",
            (today, "IAU", "BUY", 3, 80.0, "manual"),
        )
    res = check_guidance_adherence(db_path=db)
    assert len(res["followed"]) == 1
    assert res["followed"][0]["ticker"] == "IAU,SGOV"
    assert res["expired"] == [] and res["open"] == []


def test_check_adherence_expired(tmp_path):
    db = tmp_path / "t.db"
    past = "2020-01-01"
    save_guidance(
        [{"ticker": "NVDA", "action": "减仓", "target": "减至<15%", "due_by": past}],
        source="weekly", db_path=db,
    )
    # 无匹配操作 + 已过 due_by → expired
    res = check_guidance_adherence(db_path=db)
    assert len(res["expired"]) == 1 and res["expired"][0]["ticker"] == "NVDA"
    assert res["followed"] == []


def test_check_adherence_open(tmp_path):
    db = tmp_path / "t.db"
    future = (datetime.today() + timedelta(days=14)).strftime("%Y-%m-%d")
    save_guidance(
        [{"ticker": "QQQM,VOO", "action": "补仓", "target": "补被动桶", "due_by": future}],
        source="weekly", db_path=db,
    )
    # 未到期、无操作 → 仍 open
    res = check_guidance_adherence(db_path=db)
    assert len(res["open"]) == 1 and res["followed"] == [] and res["expired"] == []

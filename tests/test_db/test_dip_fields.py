# tests/test_db/test_dip_fields.py
"""order5：dip_alerts action 决策五字段落库 + 迁移测试（save 路径不触网）。"""
from finance_agent.db import tracker

_FIVE = ("action", "action_reason", "add_trigger", "add_limit", "invalidation")

_OLD_DIP_DDL = """
CREATE TABLE dip_alerts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker            TEXT NOT NULL,
    market            TEXT NOT NULL DEFAULT 'us',
    alerted_at        TEXT DEFAULT (datetime('now')),
    drop_pct          REAL,
    price_at_alert    REAL,
    opportunity       TEXT,
    thesis_intact     INTEGER,
    drop_reason       TEXT,
    price_24h         REAL,
    return_24h        REAL,
    price_7d          REAL,
    return_7d         REAL
);
"""


def test_dip_migration_idempotent(tmp_path):
    """旧 13 列表 + 史前行 → init_db 跑两次 → 5 新列齐全、旧行新列全 NULL。"""
    db = tmp_path / "t.db"
    with tracker._conn(db) as con:
        con.executescript(_OLD_DIP_DDL)
        con.execute(
            "INSERT INTO dip_alerts(ticker, market, drop_pct, price_at_alert) "
            "VALUES('NVDA', 'us', -8.0, 100.0)"
        )
    tracker.init_db(db)
    tracker.init_db(db)   # 幂等
    with tracker._conn(db) as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info(dip_alerts)").fetchall()}
        assert set(_FIVE) <= cols
        row = con.execute("SELECT * FROM dip_alerts").fetchone()
    for c in _FIVE:
        assert row[c] is None   # 史前行保持 NULL（≠''，语义是"落库功能上线前"）


def test_save_dip_alert_full(tmp_path):
    db = tmp_path / "t.db"
    analysis = {
        "opportunity": "高", "thesis_intact": True, "drop_reason": "大盘恐慌，非基本面恶化",
        "action": "加仓", "action_reason": "thesis 完好，情绪驱动下跌",
        "add_trigger": "回踩 100 企稳后", "add_limit": "不超过现有仓位的30%",
        "invalidation": "跌破前低且下季指引转弱",
    }
    tracker.save_dip_alert("NVDA", "us", -8.0, 100.0, analysis, db_path=db)
    with tracker._conn(db) as con:
        row = con.execute("SELECT * FROM dip_alerts").fetchone()
    assert row["action"] == "加仓"
    assert row["action_reason"] == "thesis 完好，情绪驱动下跌"
    assert row["add_trigger"] == "回踩 100 企稳后"
    assert row["add_limit"] == "不超过现有仓位的30%"
    assert row["invalidation"] == "跌破前低且下季指引转弱"


def test_save_dip_alert_missing_or_none(tmp_path):
    """缺 key 与显式 None（LLM 输出 null 经 json.loads）一律存 ''，NULL 留给史前行。"""
    db = tmp_path / "t.db"
    tracker.save_dip_alert("X", "us", -10.0, 50.0,
                           {"thesis_intact": False, "action": None}, db_path=db)
    with tracker._conn(db) as con:
        row = con.execute("SELECT * FROM dip_alerts").fetchone()
    for c in _FIVE:
        assert row[c] == ""
    assert row["thesis_intact"] == 0   # 显式 False → 0


def test_save_dip_alert_intact_three_state(tmp_path):
    """thesis_intact 缺键/显式 null → 存 NULL（classify_dip 落'待观察'），不许折叠成 0。"""
    from finance_agent.value.metrics import classify_dip, DIP_BUCKET_WATCH
    db = tmp_path / "t.db"
    tracker.save_dip_alert("A", "us", -9.0, 80.0, {"drop_reason": "大盘恐慌"}, db_path=db)       # 缺键
    tracker.save_dip_alert("B", "us", -9.0, 80.0, {"thesis_intact": None}, db_path=db)            # 显式 null
    with tracker._conn(db) as con:
        rows = con.execute("SELECT ticker, thesis_intact FROM dip_alerts ORDER BY ticker").fetchall()
    assert rows[0]["thesis_intact"] is None and rows[1]["thesis_intact"] is None
    assert classify_dip(rows[0]["thesis_intact"], "大盘恐慌") == DIP_BUCKET_WATCH

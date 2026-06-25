# tests/test_db/test_accuracy_dedup.py
"""日报/周报命中率必须与价值体检同口径去重（审计 2026-06-24 发现 bug#1）。

病灶：accuracy_summary / weekly_accuracy_summary 直接 count 所有 recommendations 行、
未去重。日报每天重发同票，跨天产生多行（同天才幂等），几只反复推荐的票把命中率灌偏，
且与价值体检（已去重）口径不一致 → 日报和体检卡的「命中率」对不上。
修复=两者共用 tracker._dedup_weekly（滚动7天·按方向），数字终于能对上。
"""
from finance_agent.db import tracker
from finance_agent.db.tracker import accuracy_summary, weekly_accuracy_summary


def _ins(con, date, ticker, rec, ret7, pc=""):
    outcome = "正确" if (rec == "买入" and ret7 > 0) or (rec in ("减仓", "卖出") and ret7 < 0) else "错误"
    con.execute(
        "INSERT INTO recommendations(date,ticker,recommendation,position_change,"
        "price_at_rec,market,return_7d,benchmark_return_7d,outcome) VALUES(?,?,?,?,?,?,?,?,?)",
        (date, ticker, rec, pc, 100.0, "us", ret7, 0.0, outcome),
    )


def test_weekly_winrate_dedups_repeated_ticker(tmp_path):
    """同票 07709 减仓连发 4 天(跨周一边界) + 1 只真正不同的票 → 去重后只算 2 个独立信号。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    import datetime
    recent = (datetime.date.today() - datetime.timedelta(days=8)).isoformat()
    with tracker._conn(db) as con:
        # 07709 减仓：周日+周一+周二+周三连发，卖飞(ret>0=错)；本应折成 1 条
        base = datetime.date.today() - datetime.timedelta(days=9)
        for k in range(4):
            d = (base + datetime.timedelta(days=k)).isoformat()
            _ins(con, d, "07709", "减仓", 20.0)      # 卖飞 → 错误
        _ins(con, recent, "AAA", "买入", 5.0)         # 另一只票，买对
    w = weekly_accuracy_summary(db_path=db)
    # 去重后：07709 减仓(错) 1 条 + AAA 买入(对) 1 条 = total 2、win 1 → 50%
    assert w["total"] == 2
    assert w["win_rate"] == 50


def test_daily_accuracy_dedups_repeated_ticker(tmp_path):
    db = tmp_path / "t.db"
    tracker.init_db(db)
    import datetime
    base = datetime.date.today() - datetime.timedelta(days=9)
    with tracker._conn(db) as con:
        for k in range(4):                            # 同票连发 4 天 → 折 1
            d = (base + datetime.timedelta(days=k)).isoformat()
            _ins(con, d, "07709", "减仓", 20.0)       # 卖飞 → 错误
        _ins(con, base.isoformat(), "AAA", "买入", 5.0)   # 买对
    txt = accuracy_summary(days=30, db_path=db)
    # 去重后方向性判定：判对 1(AAA) / 判错 1(07709)，而非判错 4
    assert "判对1" in txt and "判错1" in txt

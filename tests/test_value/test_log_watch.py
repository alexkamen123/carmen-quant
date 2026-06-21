# tests/test_value/test_log_watch.py
"""阶段1：log-watch CLI —— 影子选股裁决落库（is_watch=1，不花真钱考选股眼光）。
覆盖：可证伪性闸门（拒非法 rec）、方向性影子写入、CLI 层 (date,ticker) 去重保护、
与真实账户口径隔离（不进 is_watch=0 的 dir_rows）。"""
from typer.testing import CliRunner

from finance_agent import main
from finance_agent.db import tracker
from finance_agent.value.metrics import compute_value_metrics

runner = CliRunner()


def _setup_db(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    tracker.init_db(db)
    monkeypatch.setattr(main, "DB_PATH", str(db))
    return db


def test_log_watch_writes_directional_shadow(tmp_path, monkeypatch):
    db = _setup_db(tmp_path, monkeypatch)
    res = runner.invoke(main.app, ["log-watch", "GS", "买入",
                                   "--price", "650", "--date", "2026-06-21"])
    assert res.exit_code == 0, res.output
    with tracker._conn(db) as con:
        row = con.execute(
            "SELECT recommendation, is_watch, price_at_rec FROM recommendations "
            "WHERE date='2026-06-21' AND ticker='GS'").fetchone()
    assert row["is_watch"] == 1 and row["recommendation"] == "买入"
    assert abs(row["price_at_rec"] - 650) < 1e-6
    # 口径隔离：影子方向裁决绝不混进真实账户操作轨 dir_rows（gate.n_dir）
    m = compute_value_metrics(db)
    assert m["gate"]["n_dir"] == 0
    # 未回填的影子票不进选股记分牌（没满 7 天不能算分，符合预期）
    assert m["shadow_picks"]["n"] == 0
    # 回填 7 日后，影子方向裁决方可被评分，且仍只在 shadow 栏、不污染操作轨
    with tracker._conn(db) as con:
        con.execute("UPDATE recommendations SET return_7d=8.0, benchmark_return_7d=2.0, "
                    "price_7d=702 WHERE date='2026-06-21' AND ticker='GS'")
    m2 = compute_value_metrics(db)
    assert m2["shadow_picks"]["n"] == 1 and m2["shadow_picks"]["correct"] == 1
    assert m2["gate"]["n_dir"] == 0   # 仍与操作轨隔离


def test_log_watch_rejects_invalid_rec(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    res = runner.invoke(main.app, ["log-watch", "PLTR", "估值偏贵待回调",
                                   "--price", "150", "--date", "2026-06-21"])
    assert res.exit_code == 1
    assert "rec 必须是" in res.output


def test_log_watch_dedup_skips_clash(tmp_path, monkeypatch):
    db = _setup_db(tmp_path, monkeypatch)
    # 预置当天该票一条 is_watch=0 日报行，模拟与影子写入冲突
    with tracker._conn(db) as con:
        con.execute(
            "INSERT INTO recommendations(date,ticker,recommendation,market,is_watch) "
            "VALUES('2026-06-21','GS','持有','us',0)")
    res = runner.invoke(main.app, ["log-watch", "GS", "买入",
                                   "--price", "650", "--date", "2026-06-21"])
    assert res.exit_code == 0
    assert "跳过" in res.output
    with tracker._conn(db) as con:
        n = con.execute("SELECT COUNT(*) c FROM recommendations "
                        "WHERE date='2026-06-21' AND ticker='GS'").fetchone()["c"]
    assert n == 1   # 未新增，原 is_watch=0 行保留

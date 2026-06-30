# tests/test_backtest/test_oos_monitor.py
"""cycle15 walk-forward OOS 监视器：每月在最新数据上重跑方案A样本外验证，
追踪 regime-vs-static 增量随时间是否衰减，衰减就告警。

诚实核心：护栏只在跌市起作用 → 涨市样本外没跌市可测 → 跌市样本不足时裁决
insufficient_regime_data(不下结论、不误报)，只有跌市数据够+连续K月regime不再赢static 才告警。"""
import json

from finance_agent.backtest import oos_monitor as om


def _row(date, edge, n_down, horizon=7):
    """造一条月度 OOS 记录：regime_edge=regime_alpha-static_alpha，n_down=跌市笔数。"""
    return {"date": date, "horizon": horizon, "naive_alpha": 0.3,
            "static_alpha": 0.4, "regime_alpha": 0.4 + edge, "regime_edge": edge,
            "n_folds": 5, "folds_regime_wins": 5 if edge > 0 else 0,
            "n_trades": 1000, "n_down": n_down, "verdict": "x"}


def _write(log, rows):
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_decay_insufficient_when_few_down_samples(tmp_path):
    """涨市防误报：近月跌市笔数都 < min_down → insufficient_regime_data，绝不报衰减。"""
    log = tmp_path / "oos.jsonl"
    _write(log, [_row("2026-05-01", -0.2, 5), _row("2026-06-01", -0.3, 8),
                 _row("2026-07-01", -0.1, 10)])   # edge 都负，但跌市样本太少
    r = om.oos_decay_verdict(log, horizon=7, k_consecutive=3, min_down=30)
    assert r["status"] == "insufficient_regime_data"


def test_decay_healthy_when_regime_still_beats(tmp_path):
    """跌市数据够 + regime 仍赢 static → healthy。"""
    log = tmp_path / "oos.jsonl"
    _write(log, [_row("2026-05-01", 0.12, 100), _row("2026-06-01", 0.09, 120),
                 _row("2026-07-01", 0.15, 110)])
    r = om.oos_decay_verdict(log, horizon=7, k_consecutive=3, min_down=30)
    assert r["status"] == "healthy"
    assert r["n_testable"] == 3


def test_decay_alerts_on_consecutive_loss(tmp_path):
    """跌市数据够 + 连续 K 月 regime 不再赢 static(edge<=0) → decaying 告警。"""
    log = tmp_path / "oos.jsonl"
    _write(log, [_row("2026-05-01", -0.05, 100), _row("2026-06-01", -0.02, 120),
                 _row("2026-07-01", -0.08, 110)])
    r = om.oos_decay_verdict(log, horizon=7, k_consecutive=3, min_down=30)
    assert r["status"] == "decaying"


def test_decay_alerts_when_edge_slides_into_noise_band(tmp_path):
    """对自己狠(评审#1)：edge 全>0 但跌进当初启用噪声带(<+0.05)→不再是真增量→decaying。
    衰减阈值须与启用门槛(regime>static+0.05)对称，否则 edge 烂到 +0.02 还假装 healthy。"""
    log = tmp_path / "oos.jsonl"
    _write(log, [_row("2026-05-01", 0.03, 100), _row("2026-06-01", 0.02, 120),
                 _row("2026-07-01", 0.01, 110)])   # 全正但都在噪声带内
    r = om.oos_decay_verdict(log, horizon=7, k_consecutive=3, min_down=30)
    assert r["status"] == "decaying"


def test_decay_not_triggered_if_one_recent_beats(tmp_path):
    """近 K 月里只要有一月 regime 还赢 → 不算连续衰减 → healthy。"""
    log = tmp_path / "oos.jsonl"
    _write(log, [_row("2026-05-01", -0.05, 100), _row("2026-06-01", 0.10, 120),
                 _row("2026-07-01", -0.08, 110)])
    r = om.oos_decay_verdict(log, horizon=7, k_consecutive=3, min_down=30)
    assert r["status"] == "healthy"


def test_decay_only_counts_sufficient_down_rows(tmp_path):
    """混合：只把跌市样本够的行计入 testable，不够的跳过。"""
    log = tmp_path / "oos.jsonl"
    _write(log, [_row("2026-03-01", -0.05, 100), _row("2026-04-01", -0.02, 5),
                 _row("2026-05-01", -0.03, 100), _row("2026-06-01", -0.06, 8),
                 _row("2026-07-01", -0.07, 100)])
    # testable 行：03/05/07(n_down=100)，连续3条 edge 都负 → decaying
    r = om.oos_decay_verdict(log, horizon=7, k_consecutive=3, min_down=30)
    assert r["status"] == "decaying"
    assert r["n_testable"] == 3


def test_count_down_trades():
    trades = [("A", "mom", "down", -0.1), ("B", "rsi", "up", 0.2),
              ("C", "ma", "down", 0.0)]
    assert om._count_down_trades(trades) == 2


def test_oos_snapshot_row_schema():
    """快照：每 horizon 一行，含日期/两轴alpha/regime_edge/跌市笔数/裁决（注入桩测纯编排）。"""
    trades = [("A", "mom", "down", -0.1), ("B", "rsi", "up", 0.2),
              ("C", "ma", "down", 0.0)]   # 2 笔跌市
    build_stub = lambda pm, bench, fns, horizon, regime_ma: trades
    eval_stub = lambda tr, tks, k: {
        "naive_alpha": 0.30, "static_alpha": 0.40, "regime_alpha": 0.55,
        "n_folds": 5, "folds_regime_wins": 5, "verdict": "regime_adds_value"}
    rows = om.oos_snapshot({"A": 1, "B": 1, "C": 1}, object(), {}, today="2026-07-01",
                           horizons=(7, 30), build_fn=build_stub, eval_fn=eval_stub)
    assert len(rows) == 2
    r = rows[0]
    assert r["date"] == "2026-07-01" and r["horizon"] == 7
    assert r["regime_edge"] == 0.15        # 0.55 - 0.40
    assert r["n_down"] == 2
    assert r["n_trades"] == 3
    assert r["verdict"] == "regime_adds_value"


def test_oos_snapshot_edge_none_when_alpha_missing():
    """alpha 缺失(样本太少)→ regime_edge=None，不强行算。"""
    build_stub = lambda pm, bench, fns, horizon, regime_ma: []
    eval_stub = lambda tr, tks, k: {
        "naive_alpha": None, "static_alpha": None, "regime_alpha": None,
        "n_folds": 0, "folds_regime_wins": 0, "verdict": "insufficient"}
    rows = om.oos_snapshot({"A": 1}, object(), {}, today="2026-07-01",
                           horizons=(7,), build_fn=build_stub, eval_fn=eval_stub)
    assert rows[0]["regime_edge"] is None


def test_record_oos_snapshot_appends(tmp_path):
    log = tmp_path / "oos.jsonl"
    om.record_oos_snapshot([{"date": "2026-07-01", "horizon": 7, "regime_edge": 0.1}], log)
    om.record_oos_snapshot([{"date": "2026-08-01", "horizon": 7, "regime_edge": 0.2}], log)
    rows = [json.loads(l) for l in log.read_text().splitlines()]
    assert len(rows) == 2 and rows[1]["date"] == "2026-08-01"


def test_run_oos_monitor_idempotent_same_month(tmp_path):
    """幂等：本月已有记录 → 早返回 skipped、不联网重跑（防月报重跑灌库）。"""
    log = tmp_path / "oos.jsonl"
    om.record_oos_snapshot([{"date": "2026-07-01", "horizon": 7, "regime_edge": 0.1,
                             "n_down": 5}], log)
    # today 落在同月 → 命中幂等、不触发 fetch（若触发网络会很慢/失败，此处应秒回）
    out = om.run_oos_monitor(today="2026-07-28", log_path=log)
    assert out["skipped"] == "already_recorded_this_month"
    assert out["rows"] == []

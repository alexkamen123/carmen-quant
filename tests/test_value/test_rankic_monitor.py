# tests/test_value/test_rankic_monitor.py
"""P1c 月度 RankIC 自检：月度算「建议方向序 vs 后续超额序」的秩相关(Spearman RankIC)，
连续2个可测月 IC<0.03 → 推飞书提醒复核策略。纯观测·默认on·不改任何线上建议。
诚实核心：样本不足(n<30 或方向性<10)→ insufficient_sample 不下结论，宁可不判也不误报。"""
import json

from finance_agent.value import rankic_monitor as rm


def test_spearman_perfect_positive():
    assert rm.spearman_rank_ic([1, 2, 3, 4, 5], [10, 20, 30, 40, 50]) == 1.0


def test_spearman_perfect_negative():
    assert rm.spearman_rank_ic([1, 2, 3], [3, 2, 1]) == -1.0


def test_spearman_too_few():
    assert rm.spearman_rank_ic([1], [2]) is None


def test_spearman_zero_variance_none():
    """一侧全同值(秩方差0)→ None，不返 0 假装无关。"""
    assert rm.spearman_rank_ic([5, 5, 5], [1, 2, 3]) is None


def test_spearman_ties_average_rank():
    """并列取平均秩：单调含并列仍 = 1.0。"""
    assert rm.spearman_rank_ic([1, 1, 2, 2, 3], [1, 1, 2, 2, 3]) == 1.0


def test_direction_score():
    assert rm._direction_score("买入", "") == 1
    assert rm._direction_score("减仓", "") == -1
    assert rm._direction_score("卖出", "") == -1
    assert rm._direction_score("持有", "小加") == 0    # 持有+小加语义是持有
    assert rm._direction_score("观望", "") == 0


def _rows(n_buy, n_sell, buy_alpha, sell_alpha, n_hold=0):
    rows = []
    rows += [{"recommendation": "买入", "position_change": "",
              "return_7d": buy_alpha, "benchmark_return_7d": 0.0} for _ in range(n_buy)]
    rows += [{"recommendation": "卖出", "position_change": "",
              "return_7d": sell_alpha, "benchmark_return_7d": 0.0} for _ in range(n_sell)]
    rows += [{"recommendation": "持有", "position_change": "",
              "return_7d": 1.0, "benchmark_return_7d": 0.0} for _ in range(n_hold)]
    return rows


def test_snapshot_insufficient_below_min_n():
    """总样本 < 30 → insufficient_sample。"""
    snap = rm.monthly_rankic_snapshot("2026-07-01", rows_fn=lambda: _rows(5, 5, 2.0, -2.0))
    assert snap["verdict"] == "insufficient_sample" and snap["ic"] is None


def test_snapshot_insufficient_below_min_directional():
    """总样本够但方向性 < 10 → insufficient_sample。"""
    snap = rm.monthly_rankic_snapshot("2026-07-01", rows_fn=lambda: _rows(4, 4, 2.0, -2.0, n_hold=30))
    assert snap["n"] >= 30 and snap["n_directional"] == 8
    assert snap["verdict"] == "insufficient_sample"


def test_snapshot_measured_positive_ic():
    """看多→高超额、看空→低超额 → IC 正(方向有排序力)。"""
    snap = rm.monthly_rankic_snapshot("2026-07-01", rows_fn=lambda: _rows(15, 15, 2.0, -2.0))
    assert snap["verdict"] == "measured" and snap["ic"] > 0.5


def test_snapshot_measured_negative_ic():
    """方向反着来(看多却跑输、看空却跑赢)→ IC 负(系统性反向，该告警)。"""
    snap = rm.monthly_rankic_snapshot("2026-07-01", rows_fn=lambda: _rows(15, 15, -2.0, 2.0))
    assert snap["verdict"] == "measured" and snap["ic"] < -0.5


def _write(log, rows):
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _snap(date, ic, verdict="measured"):
    return {"date": date, "n": 40, "n_directional": 20, "ic": ic, "verdict": verdict}


def test_record_appends(tmp_path):
    log = tmp_path / "r.jsonl"
    rm.record_rankic_snapshot(_snap("2026-06-01", 0.1), log)
    rm.record_rankic_snapshot(_snap("2026-07-01", 0.2), log)
    assert len(log.read_text().splitlines()) == 2


def test_decay_insufficient_history(tmp_path):
    """可测月 < 2 → insufficient_history，不下结论。"""
    log = tmp_path / "r.jsonl"
    _write(log, [_snap("2026-07-01", 0.01)])
    assert rm.rankic_decay_verdict(log)["status"] == "insufficient_history"


def test_decay_decaying(tmp_path):
    """连续 2 个可测月 IC < 0.03 → decaying。"""
    log = tmp_path / "r.jsonl"
    _write(log, [_snap("2026-06-01", 0.02), _snap("2026-07-01", 0.01)])
    assert rm.rankic_decay_verdict(log)["status"] == "decaying"


def test_decay_healthy_when_recent_beats(tmp_path):
    """最近 2 个可测月里有一月 IC ≥ 0.03 → healthy。"""
    log = tmp_path / "r.jsonl"
    _write(log, [_snap("2026-06-01", 0.02), _snap("2026-07-01", 0.08)])
    assert rm.rankic_decay_verdict(log)["status"] == "healthy"


def test_decay_only_counts_measured(tmp_path):
    """insufficient 月不计入可测月序列。"""
    log = tmp_path / "r.jsonl"
    _write(log, [_snap("2026-05-01", 0.01),
                 _snap("2026-06-01", None, "insufficient_sample"),
                 _snap("2026-07-01", 0.02)])
    v = rm.rankic_decay_verdict(log)
    assert v["status"] == "decaying" and v["n_measured"] == 2


def test_run_idempotent_same_month(tmp_path):
    """同月已记 → 早返回 skipped、不重跑。"""
    log = tmp_path / "r.jsonl"
    rm.record_rankic_snapshot(_snap("2026-07-01", 0.1), log)
    out = rm.run_rankic_monitor(today="2026-07-20", log_path=log,
                                rows_fn=lambda: _rows(15, 15, 2.0, -2.0))
    assert out["skipped"] == "already_recorded_this_month"
    assert len(log.read_text().splitlines()) == 1   # 未追加


def test_alert_card_is_plain_language():
    """视觉改版：RankIC 衰减卡说人话——保留全部数字，黑话降为小字注释。"""
    from finance_agent.value.rankic_monitor import build_rankic_alert_card, IC_THRESHOLD
    verdict = {"status": "decaying", "k_needed": 2, "recent_ics": [0.02, -0.01]}
    card = build_rankic_alert_card(verdict)
    title = card["header"]["title"]["content"]
    divs = [e for e in card["elements"] if e.get("tag") == "div"]
    notes = [e for e in card["elements"] if e.get("tag") == "note"]
    main = "\n".join(d["text"]["content"] for d in divs)
    note = "\n".join(n["elements"][0]["content"] for n in notes)
    # 主句说人话：不裸露"秩相关/排序力"，标题讲"方向判断"
    assert "方向判断" in title
    assert "秩相关" not in main and "排序力" not in main
    # 数字不折损：月数 + 两个 IC 值 + 健康线阈值都在
    assert "2 个月" in main
    assert "+0.020" in main and "-0.010" in main
    assert str(IC_THRESHOLD) in main
    # 黑话定义 + "不自动改建议"降到小字注释
    assert "RankIC" in note and "不会自动改" in note

# tests/test_db/test_live_feedback.py
"""order7：L2b 实盘反馈（get_live_feedback / format_live_feedback）测试（零网络）。"""
from finance_agent.db import tracker


def _seed(db, ticker, rec=None, pos=None, ret=None, date="2026-06-01"):
    with tracker._conn(db) as con:
        con.execute(
            "INSERT INTO recommendations(date, ticker, recommendation, position_change, "
            "return_7d, market) VALUES(?, ?, ?, ?, ?, 'us')",
            (date, ticker, rec, pos, ret),
        )


def test_gate_below_min_n(tmp_path):
    """合并方向样本 < 3 → get 返回 None，format 返回空串（0 字符注入）。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    _seed(db, "NVDA", rec="买入", ret=2.0)
    _seed(db, "NVDA", rec="减仓", ret=1.0)
    assert tracker.get_live_feedback("NVDA", db_path=db) is None
    assert tracker.format_live_feedback("NVDA", db_path=db) == ""


def test_buy_only_three_wins(tmp_path):
    """3 条买入均涨 → win_rate=100，format 含'买入信号3次'，sell 段静默。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    for r in (1.0, 2.0, 3.0):
        _seed(db, "NVDA", rec="买入", ret=r)
    fb = tracker.get_live_feedback("NVDA", db_path=db)
    assert fb["buy"] == {"n": 3, "win_rate": 100, "avg": 2.0}
    assert fb["sell"] is None
    out = tracker.format_live_feedback("NVDA", db_path=db)
    assert "买入信号3次" in out and "胜率100%" in out
    assert "减仓/卖出" not in out
    assert "小样本参考" in out          # n_total=3 < 10


def test_sell_positive_flags_sold_early(tmp_path):
    """减仓后 return_7d 为正 → 含'系统性卖早'（卖飞签名回流）。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    for r in (10.0, 25.0, 22.0):
        _seed(db, "TSLA", rec="减仓", ret=r)
    out = tracker.format_live_feedback("TSLA", db_path=db)
    assert "系统性卖早" in out and "+19.0%" in out


def test_sell_negative_no_flag(tmp_path):
    """卖出后续为负（卖对了）→ 不许倒打'卖早'标签。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    for r in (-3.0, -5.0, -1.0):
        _seed(db, "X", rec="卖出", ret=r)
    out = tracker.format_live_feedback("X", db_path=db)
    assert "系统性卖早" not in out and "-3.0%" in out


def test_no_records_returns_empty(tmp_path):
    db = tmp_path / "t.db"
    tracker.init_db(db)
    assert tracker.get_live_feedback("ZZZZ", db_path=db) is None
    assert tracker.format_live_feedback("ZZZZ", db_path=db) == ""


def test_position_change_counts_as_buy(tmp_path):
    """大加/小加 position_change 也算买入方向（与 ticker_signal_stats 同口径）。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    _seed(db, "MU", rec="持有", pos="大加（+10%以上）", ret=5.0)
    _seed(db, "MU", rec="持有", pos="小加（+5~10%）", ret=-2.0)
    _seed(db, "MU", rec="买入", ret=1.0)
    fb = tracker.get_live_feedback("MU", db_path=db)
    assert fb["buy"]["n"] == 3 and fb["buy"]["win_rate"] == 67


def test_settings_disabled_silences_format(tmp_path, monkeypatch):
    """live_feedback_injection.enabled=false → format 返回空串（一键关）。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    for r in (1.0, 2.0, 3.0):
        _seed(db, "NVDA", rec="买入", ret=r)
    monkeypatch.setattr(tracker, "_load_settings_block",
                        lambda key, defaults: {"enabled": False})
    assert tracker.format_live_feedback("NVDA", db_path=db) == ""
    # get_live_feedback 不受开关影响（数据层 vs 注入层分离）
    assert tracker.get_live_feedback("NVDA", db_path=db) is not None


def test_contradictory_row_counted_once_bullish_first(tmp_path):
    """对抗审查 P1 回归：recommendation='买入' + position_change='减仓' 的矛盾行
    只计入 buy 方向（bullish 优先，与 _determine_outcome 同语义），不污染 sell 均值。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    _seed(db, "X", rec="买入", pos="减仓", ret=8.0)    # 矛盾行
    _seed(db, "X", rec="买入", ret=1.0)
    _seed(db, "X", rec="卖出", ret=-2.0)
    fb = tracker.get_live_feedback("X", db_path=db)
    assert fb["n_total"] == 3                      # 不许 4（双重计入）
    assert fb["buy"]["n"] == 2
    assert fb["sell"] == {"n": 1, "avg_next": -2.0}   # 矛盾行的 +8 没混进 sell


def test_per_direction_small_sample_tag(tmp_path):
    """对抗审查 P2 回归：n_total>=10 但单方向 n<5 → 该方向独立标注，不许裸出单例胜率。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    _seed(db, "Y", rec="买入", ret=3.0)                # n_buy=1
    for i in range(9):
        _seed(db, "Y", rec="减仓", ret=-1.0, date=f"2026-05-{i+1:02d}")
    out = tracker.format_live_feedback("Y", db_path=db)
    assert "买入信号1次" in out and "仅1次，参考意义有限" in out
    assert "小样本参考，权重宜低" not in out          # n_total=10 总标注不触发，方向标注兜住


def test_asof_excludes_immature_window(tmp_path):
    """回测 as-of：rec 日距 asof < 14 自然日的行（窗口未走完）必须排除——未来函数生命线。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    # 3 条老行（asof 时窗口已完成）+ 2 条新行（asof 时结果还看不到）
    for d, r in (("2026-05-11", -3.0), ("2026-05-12", -4.0), ("2026-05-13", -5.0)):
        _seed(db, "NVDA", rec="买入", ret=r, date=d)
    for d, r in (("2026-05-25", 50.0), ("2026-05-28", 60.0)):
        _seed(db, "NVDA", rec="买入", ret=r, date=d)

    fb = tracker.get_live_feedback("NVDA", db_path=db, asof="2026-06-01")
    assert fb["buy"]["n"] == 3                  # 只见老行
    assert fb["buy"]["avg"] == -4.0             # +50/+60 的未来结果没混进来
    # 边界：恰好 14 天 = 已成熟，可纳入
    fb2 = tracker.get_live_feedback("NVDA", db_path=db, asof="2026-06-08")
    assert fb2["buy"]["n"] == 4                 # 05-25 距 06-08 恰 14 天 → 纳入
    # asof=None 仍是全量（生产路径不变）
    fb3 = tracker.get_live_feedback("NVDA", db_path=db)
    assert fb3["buy"]["n"] == 5


def test_asof_gate_silences_early_history(tmp_path):
    """asof 早于样本积累期 → 闸门静默（回测里 5 月应是对照时代）。"""
    db = tmp_path / "t.db"
    tracker.init_db(db)
    for d in ("2026-05-11", "2026-05-12", "2026-05-13"):
        _seed(db, "MU", rec="买入", ret=1.0, date=d)
    assert tracker.get_live_feedback("MU", db_path=db, asof="2026-05-15") is None
    assert tracker.format_live_feedback("MU", db_path=db, asof="2026-05-15") == ""


def test_large_sample_no_small_tag(tmp_path):
    db = tmp_path / "t.db"
    tracker.init_db(db)
    for i in range(12):
        _seed(db, "AAPL", rec="买入", ret=float(i - 6), date=f"2026-05-{i+1:02d}")
    out = tracker.format_live_feedback("AAPL", db_path=db)
    assert "小样本参考" not in out

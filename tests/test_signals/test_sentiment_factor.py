# tests/test_signals/test_sentiment_factor.py
"""P1b 新闻情绪因子化：DeepSeek 评分从「点状用完即丢」→ 全量落库 news_sentiment_scores，
聚合成按 ticker 滚动情绪因子([-1,1] + 7日均值 + 趋势)，仅「情绪偏负下滑 + 技术动量转负」
双确认成立才给减仓提示。双 flag 分层：news_sentiment_factor(落库+快照)纯观测默认 on；
sentiment_double_confirm_alert(注入 PM)改核心建议默认 off。
诚实铁律：归一化是启发式非统计量；n<3 主动闭嘴返 None；提示只单向收紧绝不构成加仓/买入依据。"""
import sqlite3

from finance_agent.signals import sentiment_factor as sf


def _rows(db):
    con = sqlite3.connect(db)
    try:
        try:
            return con.execute(
                "SELECT ticker, key, date, score FROM news_sentiment_scores ORDER BY date"
            ).fetchall()
        except sqlite3.OperationalError:
            return []   # 表不存在 = 从未写入过任何行
    finally:
        con.close()


def _record(db, ticker="NVDA", key="k1", date="2026-07-02",
            sentiment="利空", impact=6, is_peer=0):
    return sf.record_news_sentiment(
        ticker=ticker, key=key, date=date, sentiment=sentiment,
        impact=impact, title="t", market="us", is_peer=is_peer, db_path=db)


# ── 归一化：sign × clamp(impact)/10 ─────────────────────────────────────────

def test_normalize_sign_and_scale():
    assert sf.normalize_sentiment_score("利好", 8) == 0.8
    assert sf.normalize_sentiment_score("利空", 6) == -0.6
    assert sf.normalize_sentiment_score("中性", 9) == 0.0


def test_normalize_clamps_out_of_range():
    """impact 越界/非法 → clamp 到 [0,10]，未知 sentiment 按中性 0。"""
    assert sf.normalize_sentiment_score("利好", 15) == 1.0
    assert sf.normalize_sentiment_score("利空", -3) == 0.0
    assert sf.normalize_sentiment_score("看不懂", 8) == 0.0


# ── 落库：INSERT OR IGNORE 去重、解析失败不落、flag off 不写 ─────────────────

def test_record_dedup_same_ticker_key(tmp_path):
    """低分新闻不进 news_alerted、下轮会重评 → (ticker,key) 主键防重复行。"""
    db = tmp_path / "t.db"
    assert _record(db, key="k1") is True
    assert _record(db, key="k1") is False   # 重复 → 不再写
    assert len(_rows(db)) == 1


def test_record_same_key_different_date_not_deduped(tmp_path):
    """对抗审查 CONFIRMED：dedup key 只含 MM-DD 无年份，(ticker,key) 主键会把
    2027-07-02 与 2026-07-02 同 seed 的真实新闻误当重复丢弃。主键须含 date。"""
    db = tmp_path / "t.db"
    assert _record(db, key="NVDA:07-02:abc", date="2026-07-02") is True
    assert _record(db, key="NVDA:07-02:abc", date="2027-07-02") is True   # 跨年同月日 → 独立样本
    assert len(_rows(db)) == 2


def test_record_skips_failed_classification(tmp_path):
    """DeepSeek 解析失败(impact=0) → 不落库（不用假中性稀释因子）。"""
    db = tmp_path / "t.db"
    assert _record(db, impact=0) is False
    assert _rows(db) == []


def test_record_flag_off_no_write(tmp_path, monkeypatch):
    """观测 flag 关 → 完全不写（一键关无残留）。"""
    monkeypatch.setattr(sf, "sentiment_factor_enabled", lambda: False)
    db = tmp_path / "t.db"
    assert _record(db) is False
    assert _rows(db) == []


# ── 因子聚合：7日均值 + 趋势，n<3 主动闭嘴 ──────────────────────────────────

def test_compute_insufficient_returns_none(tmp_path):
    """窗口内 n<MIN_ITEMS → None（样本不足不下结论，min_items=3 早期恒 insufficient 是预期）。"""
    db = tmp_path / "t.db"
    _record(db, key="a", date="2026-07-01")
    _record(db, key="b", date="2026-07-02")
    assert sf.compute_sentiment_factor("NVDA", db_path=db, asof="2026-07-02") is None


def test_compute_mean_window_and_ticker_isolation(tmp_path):
    """均值只算窗口内本票行：8天前的行、其他票的行都不进来。"""
    db = tmp_path / "t.db"
    _record(db, key="a", date="2026-06-30", sentiment="利空", impact=6)   # -0.6
    _record(db, key="b", date="2026-07-01", sentiment="利好", impact=3)   # +0.3
    _record(db, key="c", date="2026-07-02", sentiment="中性", impact=5)   #  0.0
    _record(db, key="old", date="2026-06-24", sentiment="利空", impact=10)  # 窗口外(8天前)
    _record(db, ticker="AMD", key="x", date="2026-07-01", sentiment="利空", impact=9)  # 其他票
    f = sf.compute_sentiment_factor("NVDA", db_path=db, asof="2026-07-02")
    assert f["n"] == 3
    assert f["score_7d"] == round((-0.6 + 0.3 + 0.0) / 3, 3)


def test_compute_trend_negative_when_recent_worse(tmp_path):
    """趋势=近3天均值−前段均值：前段利好、近段转利空 → trend<0（情绪下滑）。"""
    db = tmp_path / "t.db"
    _record(db, key="a", date="2026-06-26", sentiment="利好", impact=6)   # 前段 +0.6
    _record(db, key="b", date="2026-06-27", sentiment="利好", impact=4)   # 前段 +0.4
    _record(db, key="c", date="2026-07-01", sentiment="利空", impact=6)   # 近段 -0.6
    _record(db, key="d", date="2026-07-02", sentiment="利空", impact=8)   # 近段 -0.8
    f = sf.compute_sentiment_factor("NVDA", db_path=db, asof="2026-07-02")
    assert f["trend"] == round((-0.7) - 0.5, 3)   # -1.2


def test_compute_trend_none_when_no_prior_half(tmp_path):
    """全部新闻都在近3天、前段为空 → trend=None（没有对比基线就不编趋势）。"""
    db = tmp_path / "t.db"
    for k, d in (("a", "2026-06-30"), ("b", "2026-07-01"), ("c", "2026-07-02")):
        _record(db, key=k, date=d, sentiment="利空", impact=5)
    f = sf.compute_sentiment_factor("NVDA", db_path=db, asof="2026-07-02")
    assert f["n"] == 3 and f["trend"] is None


# ── 双确认：情绪偏负且下滑 AND 技术动量转负，条件缺一不发 ────────────────────

def test_double_confirm_all_conditions_true():
    f = {"score_7d": -0.5, "trend": -0.3, "n": 4}
    assert sf.detect_double_confirm(f, macd_trend="bearish", composite_score=-0.2) is True


def test_double_confirm_any_condition_blocks():
    f = {"score_7d": -0.5, "trend": -0.3, "n": 4}
    assert sf.detect_double_confirm(None, "bearish", -0.2) is False            # 因子闭嘴
    assert sf.detect_double_confirm(f, "bullish", -0.2) is False               # 动量未转负
    assert sf.detect_double_confirm(f, "bearish", 0.3) is False                # 综合分仍偏多
    assert sf.detect_double_confirm({**f, "score_7d": 0.2}, "bearish", -0.2) is False  # 情绪不负
    assert sf.detect_double_confirm({**f, "score_7d": -0.05}, "bearish", -0.2) is False  # 负得不够(噪声带)
    assert sf.detect_double_confirm({**f, "trend": None}, "bearish", -0.2) is False    # 无趋势基线
    assert sf.detect_double_confirm({**f, "trend": 0.1}, "bearish", -0.2) is False     # 情绪在回升


# ── 注入 PM 的减仓提示：flag 默认 off 恒 ""，on 时措辞只单向收紧 ─────────────

def _seed_confirming(db):
    """构造满足双确认的情绪数据：前段中性、近段持续利空。"""
    _record(db, key="a", date="2026-06-26", sentiment="中性", impact=5)
    _record(db, key="b", date="2026-06-27", sentiment="利空", impact=4)
    _record(db, key="c", date="2026-07-01", sentiment="利空", impact=7)
    _record(db, key="d", date="2026-07-02", sentiment="利空", impact=8)


def test_note_default_off_empty(tmp_path, monkeypatch):
    """flag off → 恒 ""，strategy_evidence 逐字节零变化（不变式）。
    （2026-07-02 用户点头后仓库实际值已置 true，故 monkeypatch 锁 off 态而非依赖仓库值。）"""
    monkeypatch.setattr(sf, "double_confirm_enabled", lambda: False)
    db = tmp_path / "t.db"
    _seed_confirming(db)
    out = sf.format_sentiment_note("NVDA", macd_trend="bearish", composite_score=-0.5,
                                   db_path=db, asof="2026-07-02")
    assert out == ""


def test_note_on_confirmed_wording_one_way_only(tmp_path, monkeypatch):
    """flag on + 双确认成立 → 有内容、标注占位待校准、绝不出现加仓/买入措辞。"""
    monkeypatch.setattr(sf, "double_confirm_enabled", lambda: True)
    db = tmp_path / "t.db"
    _seed_confirming(db)
    out = sf.format_sentiment_note("NVDA", macd_trend="bearish", composite_score=-0.5,
                                   db_path=db, asof="2026-07-02")
    assert "【情绪双确认】" in out
    assert "待校准" in out or "占位" in out
    assert "加仓" not in out and "买入" not in out and "更激进" not in out


def test_note_on_but_not_confirmed_empty(tmp_path, monkeypatch):
    """flag on 但动量未转负 → ""（双确认缺一不发，降噪本意）。"""
    monkeypatch.setattr(sf, "double_confirm_enabled", lambda: True)
    db = tmp_path / "t.db"
    _seed_confirming(db)
    out = sf.format_sentiment_note("NVDA", macd_trend="bullish", composite_score=0.3,
                                   db_path=db, asof="2026-07-02")
    assert out == ""


# ── recommendations 埋点快照：供 P1c RankIC 校验 ─────────────────────────────

def test_snapshot_values_match_factor(tmp_path):
    db = tmp_path / "t.db"
    _seed_confirming(db)
    snap = sf.sentiment_snapshot("NVDA", db_path=db, asof="2026-07-02")
    f = sf.compute_sentiment_factor("NVDA", db_path=db, asof="2026-07-02")
    assert snap["sentiment_7d"] == f["score_7d"]
    assert snap["sentiment_trend"] == f["trend"]
    assert snap["sentiment_n"] == f["n"]


def test_snapshot_flag_off_or_insufficient_all_none(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    _seed_confirming(db)
    # 样本不足（无该票数据）→ 全 None
    empty = sf.sentiment_snapshot("AMD", db_path=db, asof="2026-07-02")
    assert empty == {"sentiment_7d": None, "sentiment_trend": None, "sentiment_n": None}
    # 观测 flag 关 → 全 None（列恒 NULL，与改动前一致）
    monkeypatch.setattr(sf, "sentiment_factor_enabled", lambda: False)
    off = sf.sentiment_snapshot("NVDA", db_path=db, asof="2026-07-02")
    assert off == {"sentiment_7d": None, "sentiment_trend": None, "sentiment_n": None}


def test_save_recommendations_sentiment_columns(tmp_path):
    """tracker 3 新列落库；旧调用方不带 key → NULL（向后兼容）。"""
    from finance_agent.db import tracker
    db = tmp_path / "t.db"
    tracker.init_db(db)
    tracker.save_recommendations("2026-07-02", [
        {"ticker": "NVDA", "recommendation": "持有",
         "sentiment_7d": -0.42, "sentiment_trend": -0.8, "sentiment_n": 4},
        {"ticker": "AMD", "recommendation": "持有"},   # 旧式调用无情绪 key
    ], db_path=db)
    with tracker._conn(db) as con:
        rows = {r["ticker"]: r for r in con.execute(
            "SELECT ticker, sentiment_7d, sentiment_trend, sentiment_n FROM recommendations"
        ).fetchall()}
    assert rows["NVDA"]["sentiment_7d"] == -0.42 and rows["NVDA"]["sentiment_n"] == 4
    assert rows["AMD"]["sentiment_7d"] is None and rows["AMD"]["sentiment_n"] is None

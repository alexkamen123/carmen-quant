# tests/test_db/test_reflection.py
"""P0b 反思闭环：把某票最近 N 条已结算方向性建议(含 vs 基准超额)浓缩成反思，
注入下次同 ticker 组合决策 prompt。改核心建议→flag 默认 off。零 LLM、零网络、确定性模板。
诚实铁律：措辞只单向收紧(宜更保守/可维持)，绝不出现"更激进"；未来函数靠 asof 成熟闸守死。"""
from finance_agent.db import tracker


def _seed(db, ticker, rec=None, pos=None, ret=None, bm=None,
          outcome="hit", is_watch=0, date="2026-06-01"):
    with tracker._conn(db) as con:
        con.execute(
            "INSERT INTO recommendations(date, ticker, recommendation, position_change, "
            "return_7d, benchmark_return_7d, outcome, is_watch, market) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'us')",
            (date, ticker, rec, pos, ret, bm, outcome, is_watch),
        )


def test_no_directional_returns_none(tmp_path):
    """只有持有/观望(非方向性) → get 返 None。"""
    db = tmp_path / "t.db"; tracker.init_db(db)
    _seed(db, "NVDA", rec="持有", ret=2.0, bm=1.0)
    _seed(db, "NVDA", rec="观望", ret=1.0, bm=0.5)
    assert tracker.get_recent_reflections("NVDA", db_path=db) is None


def test_buy_underperform_judged_wrong(tmp_path):
    """看多但跑输基准(alpha<0) → 判错、跑输基准。"""
    db = tmp_path / "t.db"; tracker.init_db(db)
    _seed(db, "NVDA", rec="买入", ret=1.2, bm=4.3)   # alpha=-3.1
    r = tracker.get_recent_reflections("NVDA", db_path=db)
    it = r["items"][0]
    assert it["dir"] == "B" and it["alpha"] == -3.1 and it["correct"] is False


def test_buy_beat_judged_correct(tmp_path):
    """看多且跑赢基准(alpha>0) → 判对。"""
    db = tmp_path / "t.db"; tracker.init_db(db)
    _seed(db, "NVDA", rec="买入", ret=4.0, bm=2.0)   # alpha=+2.0
    r = tracker.get_recent_reflections("NVDA", db_path=db)
    assert r["items"][0]["alpha"] == 2.0 and r["items"][0]["correct"] is True


def test_sell_correct_when_stock_falls(tmp_path):
    """减仓后该股跑输基准(alpha<0) → 卖对了(判对)。"""
    db = tmp_path / "t.db"; tracker.init_db(db)
    _seed(db, "TSLA", rec="减仓", ret=-3.0, bm=1.0)   # alpha=-4.0 → 卖对
    r = tracker.get_recent_reflections("TSLA", db_path=db)
    assert r["items"][0]["dir"] == "S" and r["items"][0]["correct"] is True


def test_benchmark_null_graceful(tmp_path):
    """无基准 → alpha=None，按绝对涨跌判对错、不崩。"""
    db = tmp_path / "t.db"; tracker.init_db(db)
    _seed(db, "X", rec="买入", ret=3.0, bm=None)
    r = tracker.get_recent_reflections("X", db_path=db)
    assert r["items"][0]["alpha"] is None and r["items"][0]["correct"] is True


def test_limit_recent_n(tmp_path):
    """只取最近 n 条方向性(n=3)。日期拉开 >7 天=独立判断，不被去重折叠。"""
    db = tmp_path / "t.db"; tracker.init_db(db)
    for i, d in enumerate(("2026-05-01", "2026-05-10", "2026-05-20", "2026-05-30", "2026-06-09")):
        _seed(db, "NVDA", rec="买入", ret=1.0 + i, bm=0.0, date=d)
    r = tracker.get_recent_reflections("NVDA", db_path=db, n=3)
    assert r["n"] == 3 and len(r["items"]) == 3
    assert r["items"][0]["date"] == "2026-06-09"   # 最近优先


def test_daily_repeats_collapse_to_one(tmp_path):
    """日报连发去重(Nit1·与命中率同口径)：同票同方向7天内连推算一条，不按N次伪膨胀。"""
    db = tmp_path / "t.db"; tracker.init_db(db)
    for d in ("2026-06-01", "2026-06-02", "2026-06-03"):
        _seed(db, "NVDA", rec="买入", ret=1.0, bm=4.0, date=d)   # 同一 thesis 连推
    r = tracker.get_recent_reflections("NVDA", db_path=db, n=3)
    assert r["n"] == 1   # 折成一条，不当 3 次独立判断


def test_conservative_lean_when_mostly_wrong(tmp_path):
    """近3次2错1对 → 宜更保守。"""
    db = tmp_path / "t.db"; tracker.init_db(db)
    _seed(db, "NVDA", rec="买入", ret=1.0, bm=4.0, date="2026-05-01")   # alpha-3 错
    _seed(db, "NVDA", rec="买入", ret=2.0, bm=5.0, date="2026-05-15")   # alpha-3 错
    _seed(db, "NVDA", rec="买入", ret=5.0, bm=1.0, date="2026-05-29")   # alpha+4 对
    r = tracker.get_recent_reflections("NVDA", db_path=db, n=3)
    assert r["n_correct"] == 1 and r["lean"] == "宜更保守"


def test_asof_excludes_immature(tmp_path):
    """未来函数生命线：asof 时点距 rec < 14 天的行被排除(窗口未走完)。"""
    db = tmp_path / "t.db"; tracker.init_db(db)
    _seed(db, "NVDA", rec="买入", ret=9.9, bm=0.0, date="2026-06-18")   # 距asof仅2天→排除
    _seed(db, "NVDA", rec="买入", ret=3.0, bm=1.0, date="2026-05-25")   # 距asof26天→保留
    r = tracker.get_recent_reflections("NVDA", db_path=db, asof="2026-06-20")
    assert r["n"] == 1 and r["items"][0]["date"] == "2026-05-25"


def test_is_watch_excluded(tmp_path):
    """影子选股(is_watch=1)不进反思(与价值体检同口径)。"""
    db = tmp_path / "t.db"; tracker.init_db(db)
    _seed(db, "NVDA", rec="买入", ret=3.0, bm=1.0, is_watch=1)
    assert tracker.get_recent_reflections("NVDA", db_path=db) is None


def test_unsettled_excluded(tmp_path):
    """outcome 为空(未结算)不进反思。"""
    db = tmp_path / "t.db"; tracker.init_db(db)
    _seed(db, "NVDA", rec="买入", ret=3.0, bm=1.0, outcome=None)
    assert tracker.get_recent_reflections("NVDA", db_path=db) is None


def test_format_default_off_empty(tmp_path, monkeypatch):
    """flag off → format 返回 ""(零变化不变式)。
    （2026-07-02 用户点头后仓库实际值已置 true，故 monkeypatch 锁 off 态而非依赖仓库值。）"""
    monkeypatch.setattr(tracker, "_load_settings_block",
                        lambda key, d: {**d, "enabled": False} if key == "reflection_injection" else d)
    db = tmp_path / "t.db"; tracker.init_db(db)
    _seed(db, "NVDA", rec="买入", ret=4.0, bm=2.0)
    assert tracker.format_reflection("NVDA", db_path=db) == ""


def test_format_on_content_and_never_aggressive(tmp_path, monkeypatch):
    """flag on：含【历史反思】、判对错、单向收紧措辞；全对→可维持，绝不出现"更激进"。"""
    monkeypatch.setattr(tracker, "_load_settings_block",
                        lambda key, d: {**d, "enabled": True} if key == "reflection_injection" else d)
    db = tmp_path / "t.db"; tracker.init_db(db)
    for i, dt in enumerate(("2026-05-01", "2026-05-15", "2026-05-29")):
        _seed(db, "NVDA", rec="买入", ret=5.0 + i, bm=1.0, date=dt)   # 全跑赢→全对（日期拉开）
    out = tracker.format_reflection("NVDA", db_path=db)
    assert "【历史反思】" in out and "判对" in out
    assert "可维持" in out
    assert "更激进" not in out and "宜更保守" not in out

from finance_agent.signals import thesis_invalidation as ti


def test_check_earnings_miss():
    assert ti.check_earnings_miss(-2.0, sigma=1.5) is True
    assert ti.check_earnings_miss(-1.0, sigma=1.5) is False
    assert ti.check_earnings_miss(None) is False
    # earnings_miss：恰好等于阈值算触发（<=）
    assert ti.check_earnings_miss(-1.5, sigma=1.5) is True


def test_check_news_negative():
    assert ti.check_news_negative(8, "利空", impact_min=7) is True
    assert ti.check_news_negative(8, "利好", impact_min=7) is False
    assert ti.check_news_negative(5, "利空", impact_min=7) is False
    assert ti.check_news_negative(None, "利空") is False


def test_check_price_break():
    assert ti.check_price_break(140.0, 150.0) is True
    assert ti.check_price_break(150.0, 150.0) is False
    assert ti.check_price_break(160.0, 150.0) is False
    assert ti.check_price_break(140.0, None) is False
    assert ti.check_price_break(None, 150.0) is False


def test_check_revenue_decline():
    assert ti.check_revenue_decline([-3.0, -1.0]) is True
    assert ti.check_revenue_decline([-3.0, 2.0]) is False
    assert ti.check_revenue_decline([-1.0]) is False
    assert ti.check_revenue_decline([]) is False


def test_check_margin_break():
    assert ti.check_margin_break(35.0, 40.0) is True
    assert ti.check_margin_break(45.0, 40.0) is False
    assert ti.check_margin_break(35.0, None) is False
    assert ti.check_margin_break(None, 40.0) is False
    # margin_break：恰好等于阈值不算（<，需真跌破）
    assert ti.check_margin_break(40.0, 40.0) is False


def test_match_triggers():
    pillars = [
        {"pillar": "毛利护城河", "trigger_type": "margin_break", "threshold": 40.0},
        {"pillar": "增长故事", "trigger_type": "revenue_decline", "threshold": None},
        {"pillar": "AI 需求", "trigger_type": "news_negative", "threshold": None},
    ]
    measured = {"margin_break": 35.0, "revenue_decline": [-3.0, -1.0]}
    hits = ti.match_triggers(pillars, measured, sigma=1.5, impact_min=7)
    got = {(h["pillar"], h["trigger_type"]) for h in hits}
    assert got == {("毛利护城河", "margin_break"), ("增长故事", "revenue_decline")}


def test_format_invalidation_alert_wording():
    note = ti.format_invalidation_alert("NVDA", "毛利护城河", "margin_break", "毛利率跌破 40%（当前 35%）")
    assert "止损复核" in note and "毛利护城河" in note and "NVDA" in note
    assert "加仓" not in note and "买入" not in note


def test_flag_default_off(monkeypatch, tmp_path):
    monkeypatch.setattr(ti, "_CONFIG_DIR", tmp_path)
    assert ti.thesis_invalidation_enabled() is False

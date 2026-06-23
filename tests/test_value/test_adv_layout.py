# tests/test_value/test_adv_layout.py
"""能力总评·明细的可读性结构：三把不同口径的尺子各用 ▍小标题分块、口径写进标题，
行内括号注解(跑赢/明显跑输/后续真跌/没卖能多赚)全部下沉到底部一条统一脚注。

病灶（用户反馈"巨乱"）：一个面板里塞三种口径，每行自带"别看错"的括号注解，
数据被注解淹没成一堵墙。修复=信息架构分块、口径上提标题、注解合并成单条脚注。
"""
from finance_agent.value.report import _adv_section


def _m(**kw):
    base = {
        "hit_rate": {"sell_right": 1, "sell_wrong": 13},
        "buy_alpha": {"n": 2, "avg": 1.3, "status": "ok"},
        "hold_quality": {"n": 42, "right": 27, "wrong": 8, "avg_alpha": 23.8,
                         "wrong_cases": [{"ticker": "QBTS", "date": "2026-06-02", "alpha": -14},
                                         {"ticker": "GOOGL", "date": "2026-05-11", "alpha": -14}]},
        "combined_alpha": {"avg": -35.5, "status": "ok"},
        "active_vs_passive": {"dca_avg": 0.0, "dca_n": 91, "active_avg": -6.1, "active_n": 13},
        "alpha_by_window": {"7d": {"alpha_avg": -5.2, "alpha_n": 16},
                            "30d": {"alpha_avg": -44.1, "alpha_n": 5}, "90d": None},
    }
    base.update(kw)
    return base


def test_three_rulers_have_explicit_kou_jing_headers():
    """三把尺各一个 ▍小标题，口径直接写进标题（不再靠行内注解区分）。"""
    txt = _adv_section(_m())
    assert txt.count("▍") == 3
    assert "口径「到今天" in txt
    assert "口径「7 日绝对收益」" in txt
    assert "口径「固定窗口定格」" in txt


def test_no_inline_gloss_parentheses():
    """行内重复注解全部移除，数据行保持干净。"""
    txt = _adv_section(_m())
    assert "幸亏拿住" not in txt and "该卖没卖" not in txt
    assert "后续真跌" not in txt and "没卖能多赚" not in txt


def test_single_consolidated_footnote():
    """所有消歧 + 免责合并成唯一一条 ⓘ 脚注。"""
    txt = _adv_section(_m())
    assert txt.count("ⓘ") == 1
    assert "别横着比" in txt          # 三尺口径不同的免责
    assert "不是一回事" in txt        # 主动 vs 信号消歧（原 behavior_isolation 要求）
    assert "你主动买入" in txt


def test_core_numbers_survive():
    """减法不能丢数字：四个关键数仍在。"""
    txt = _adv_section(_m())
    assert "+1.3%" in txt and "+23.8%" in txt and "-35.5%" in txt
    assert "拿对 27" in txt and "卖飞 13" in txt

# tests/test_value/test_summary.py
"""主屏极简结论块：三类建议各一句话+一个数(都到今天)，行动建议收尾。
用户反馈"打分艰涩、一会赢一会负看不懂"→ 主屏只留定性结论+锚点数，
combined_alpha(-38%)/分窗口/主动vs定投等会正负打架的精确数字全部下沉到折叠。
"""
from finance_agent.value.report import _summary_section


def _m(**kw):
    base = {
        "hold_quality": {"n": 42, "avg_alpha": 25.8},
        "hit_rate": {"sell_right": 1, "sell_wrong": 12},
        "buy_alpha": {"n": 2, "avg": -0.9, "status": "ok"},
        "cumulative": {"excess_pct": 4.7},
        "combined_alpha": {"avg": -38.3, "status": "ok"},
    }
    base.update(kw)
    return base


def test_three_plain_lines_with_action():
    txt = _summary_section(_m())
    assert "让你拿住的" in txt and "25.8" in txt and "✅" in txt
    assert "让你减仓的" in txt and "卖飞" in txt          # 12>1 → 大多卖飞
    assert "让你买的" in txt and "2" in txt               # 才2次
    assert "守住好仓" in txt                              # 行动建议(赚靠拿住模式)


def test_no_scary_precise_numbers_on_main():
    """主屏绝不出现 combined_alpha(-38)/分窗口/主动买入 这些正负打架的数。"""
    txt = _summary_section(_m())
    assert "-38" not in txt and "38.3" not in txt
    assert "分窗口" not in txt and "7天" not in txt
    assert "主动买入" not in txt


def test_buy_few_says_observe():
    txt = _summary_section(_m(buy_alpha={"n": 0, "status": "no_sample"}))
    assert "让你买的" in txt and "观望" in txt


def test_action_omitted_when_not_earn_by_holding():
    """非'赚靠拿住'模式(如买卖也赚)就不强行给'少折腾'。"""
    txt = _summary_section(_m(combined_alpha={"avg": 3.0, "status": "ok"}))
    assert "守住好仓" not in txt

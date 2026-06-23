# tests/test_value/test_adv_layout.py
"""能力总评·明细的可读性结构：三把不同口径的尺子各用 ▍小标题分块、用「人话」描述口径，
行内括号注解全部下沉到底部一条统一脚注。

病灶（用户反馈"巨乱" + 干净视角评审 4/10）：
1) 一个面板塞三种口径，每行自带注解 → 数据被注解淹成墙；
2) ▍标题用「口径『固定窗口定格』」这类工程黑话，散户看像乱码、零价值；
3) 满屏「超额 / 非账户加权 / n=16」黑话。
修复=分块 + 口径用人话写进标题 + 黑话全翻译成「比大盘多赚/跑输」+ 注解合并单脚注。
"""
from finance_agent.value.report import _adv_section


def _m(**kw):
    base = {
        "hit_rate": {"sell_right": 1, "sell_wrong": 13},
        "buy_alpha": {"n": 2, "avg": 1.8, "status": "ok"},
        "hold_quality": {"n": 32, "right": 24, "wrong": 8, "avg_alpha": 17.1,
                         "wrong_cases": [{"ticker": "GOOGL", "date": "2026-05-11", "alpha": -13},
                                         {"ticker": "NVDA", "date": "2026-05-18", "alpha": -10}]},
        "combined_alpha": {"avg": -24.6, "status": "ok"},
        "active_vs_passive": {"dca_avg": 0.0, "dca_n": 91, "active_avg": -6.1, "active_n": 13},
        "alpha_by_window": {"7d": {"alpha_avg": -5.2, "alpha_n": 16},
                            "30d": {"alpha_avg": -44.1, "alpha_n": 5}, "90d": None},
    }
    base.update(kw)
    return base


def test_three_rulers_blocked_with_plain_headers():
    """三把尺各一个 ▍小标题，口径用人话写（不是工程黑话）。"""
    txt = _adv_section(_m())
    assert txt.count("▍") == 3
    assert "到今天" in txt          # 尺①口径用人话
    assert "7 天" in txt            # 尺②口径用人话
    assert "第 7/30/90 天" in txt or "第 7" in txt   # 尺③口径用人话


def test_no_jargon_terms():
    """黑话全清：超额 / 非账户加权 / 固定窗口定格 / n= 一律不出现。"""
    txt = _adv_section(_m())
    assert "超额" not in txt
    assert "非账户加权" not in txt and "加权" not in txt
    assert "固定窗口定格" not in txt and "绝对收益" not in txt
    assert "n=" not in txt          # 样本数用「次」不用 n


def test_no_inline_gloss_parentheses():
    """行内重复注解全部移除。"""
    txt = _adv_section(_m())
    assert "幸亏拿住" not in txt and "该卖没卖" not in txt
    assert "后续真跌" not in txt and "没卖能多赚" not in txt


def test_single_consolidated_footnote():
    """所有消歧 + 免责合并成唯一一条 ⓘ 脚注。"""
    txt = _adv_section(_m())
    assert txt.count("ⓘ") == 1
    assert "别" in txt and "横" in txt      # 三尺口径不同别横比
    assert "不是一回事" in txt              # 主动 vs 信号消歧（behavior_isolation 要求）
    assert "你主动买入" in txt


def test_plain_language_verbs():
    """数字用「比大盘多赚/跑输大盘」人话表达，不用「超额」。"""
    txt = _adv_section(_m())
    assert "多赚" in txt                    # 买/持为正 → 多赚
    assert "跑输大盘 24.6%" in txt          # 合计为负 → 跑输大盘（语义=vs大盘，非vs持有）
    assert "拿对 24" in txt and "卖飞 13" in txt

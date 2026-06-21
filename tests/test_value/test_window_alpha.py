# tests/test_value/test_window_alpha.py
"""分窗口超额展示：小样本(n<5)不出数值，改「样本少暂不下结论」。

病灶：真实数据「30天超额 -50.4%(n=4)」吓人且不可信——4 个样本 + 异常值算出的 -50%
对用户是噪声、伤信任。修复：只有样本够(n≥5)的窗口才报数值，否则诚实说样本少。
"""
from finance_agent.value.report import _format_window_alpha


def test_keeps_big_sample_hides_tiny():
    abw = {"7d": {"alpha_avg": -5.4, "alpha_n": 15},
           "30d": {"alpha_avg": -50.4, "alpha_n": 4},
           "90d": None}
    line = _format_window_alpha(abw)
    assert line is not None
    assert "7天 -5.4%" in line          # 样本够，照报
    assert "-50.4%" not in line          # 小样本数值绝不出现（不吓人不误导）
    assert "样本少" in line and "n=4" in line


def test_none_when_all_too_small_or_missing():
    abw = {"7d": {"alpha_avg": 1.0, "alpha_n": 2}, "30d": None, "90d": None}
    assert _format_window_alpha(abw) is None   # 没有任一窗口够样本 → 整行不显示


def test_none_when_empty():
    assert _format_window_alpha({}) is None

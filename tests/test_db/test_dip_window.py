# tests/test_db/test_dip_window.py
"""暴跌预警 7天回填取价必须锚定 alert 日、且有上界，不能拿偏晚的价冒充（审计 bug#2）。

病灶：backfill_dip_outcomes 用 period="10d"(仅最近10天) + closes[index>=target].iloc[0] 无上界。
晚回填(>17天)的 alert，target_7d 落在窗口之前 → iloc[0] 抓到偏晚的价，在涨市夸大"后续上涨"，
污染暴跌预警统计（可能正是 +33%/+35% 偏高的来源）。
修复=_pick_close_on_or_after(closes, target, max_gap)：锚 target 日、超 max_gap 天无数据返 None。
"""
import pandas as pd
from finance_agent.db.tracker import _pick_close_on_or_after


def _series(dates, vals):
    return pd.Series(vals, index=pd.to_datetime(dates))


def test_picks_first_close_on_or_after_target():
    s = _series(["2026-05-10", "2026-05-11", "2026-05-12"], [100.0, 101.0, 102.0])
    # target 5-11 → 取 5-11 的 101
    assert _pick_close_on_or_after(s, pd.Timestamp("2026-05-11"), max_gap_days=5) == 101.0


def test_skips_weekend_to_next_trading_day():
    s = _series(["2026-05-08", "2026-05-11"], [100.0, 105.0])   # 周五、下周一
    # target 周六 5-9 → 取下一个交易日 5-11
    assert _pick_close_on_or_after(s, pd.Timestamp("2026-05-09"), max_gap_days=5) == 105.0


def test_returns_none_when_gap_too_large():
    """target 之后最近的 bar 距 target 超过 max_gap → 不许拿偏晚的价冒充，返回 None。"""
    s = _series(["2026-05-30"], [200.0])
    # target 5-10，最近 bar 是 5-30（20天后）→ 超 5 天上界 → None（旧 bug 会返 200 冒充）
    assert _pick_close_on_or_after(s, pd.Timestamp("2026-05-10"), max_gap_days=5) is None


def test_returns_none_when_no_bar_after_target():
    s = _series(["2026-05-01", "2026-05-02"], [100.0, 101.0])
    assert _pick_close_on_or_after(s, pd.Timestamp("2026-05-10"), max_gap_days=5) is None

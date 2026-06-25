# tests/test_value/test_dedup_rolling.py
"""去重对齐意图：滚动 7 天 · 按方向，而非 ISO 周分桶。

真 bug（审计 2026-06-24 发现）：_dedup_weekly 注释意图是「一周内反复推荐只算一次」(滚动)，
实现却用 (ticker, ISO周)。周一边界一切就漏——07709 减仓 5/17(周日,W20) 与 5/18(周一,W21)
ret/bench 完全相同(同一条日报连发)，却被当两条独立信号，-50.1 双计、灌爆 combined_alpha。

修复=滚动 7 天窗口(同票同方向 <7 天折一条) + 键含方向(买入↔减仓翻转不误折)。
"""
from finance_agent.value.metrics import _dedup_weekly


def _r(date, ticker, rec, pc=""):
    return {"date": date, "ticker": ticker, "recommendation": rec, "position_change": pc}


def test_sunday_monday_same_signal_collapses():
    """跨 ISO 周边界(周日 5/17→周一 5/18)的同票同向连发，必须折成一条。"""
    rows = [_r("2026-05-17", "07709", "减仓"), _r("2026-05-18", "07709", "减仓")]
    out = _dedup_weekly(rows)
    assert len(out) == 1
    assert out[0]["date"] == "2026-05-17"   # 取首条，确定性


def test_within_7_days_collapses_across_week():
    """同票同向 <7 天一律折一条（不管是否同 ISO 周）。"""
    rows = [_r("2026-05-17", "07709", "减仓"),
            _r("2026-05-18", "07709", "减仓"),
            _r("2026-05-19", "07709", "减仓"),
            _r("2026-05-29", "07709", "减仓"),   # 距首条 12 天 → 新信号
            _r("2026-06-01", "07709", "减仓")]   # 距 5/29 仅 3 天 → 折叠
    out = _dedup_weekly(rows)
    assert [r["date"] for r in out] == ["2026-05-17", "2026-05-29"]


def test_direction_flip_kept():
    """同票一周内『买入→减仓』方向翻转是两个真信号，不许折成一条。"""
    rows = [_r("2026-05-18", "NVDA", "买入"), _r("2026-05-20", "NVDA", "减仓")]
    out = _dedup_weekly(rows)
    assert len(out) == 2


def test_gap_over_7_days_kept():
    rows = [_r("2026-05-01", "MU", "减仓"), _r("2026-05-10", "MU", "减仓")]
    out = _dedup_weekly(rows)
    assert len(out) == 2


def test_consecutive_same_week_still_collapses():
    """老行为不回退：同票同周连推仍折一条（滚动窗口天然覆盖）。"""
    rows = [_r(d, "00700", "持有") for d in ("2026-05-04", "2026-05-05", "2026-05-06")]
    assert len(_dedup_weekly(rows)) == 1

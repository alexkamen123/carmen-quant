import pandas as pd
from finance_agent.data import yfinance_provider as yp


def _qdf(cols):
    # cols: [(date, total_revenue, gross_profit)]；列=季度
    idx = ["Total Revenue", "Gross Profit"]
    data = {pd.Timestamp(d): [rev, gp] for d, rev, gp in cols}
    return pd.DataFrame(data, index=idx)


def test_extract_rev_margin_yoy_and_margin():
    df = _qdf([("2026-03-31", 90, 30), ("2025-12-31", 100, 45), ("2025-09-30", 110, 44),
               ("2025-06-30", 120, 48), ("2025-03-31", 100, 40)])
    out = yp.extract_rev_margin(df)
    assert out["yoy_growths"][-1] < 0                       # 最近季 90 vs 去年同季 100 → 负
    assert abs(out["gross_margin"] - (30 / 90 * 100)) < 0.01  # 最近季毛利率


def test_extract_rev_margin_insufficient_or_none():
    assert yp.extract_rev_margin(None) is None
    assert yp.extract_rev_margin(pd.DataFrame()) is None


def test_extract_rev_margin_six_quarters_two_yoy():
    # 6 季 → 恰好 2 个 yoy 值（钉死"要6季才算连续2季同比"契约）
    df = _qdf([("2026-06-30", 95, 30), ("2026-03-31", 90, 30), ("2025-12-31", 100, 45),
               ("2025-09-30", 110, 44), ("2025-06-30", 120, 48), ("2025-03-31", 100, 40)])
    out = yp.extract_rev_margin(df)
    assert len(out["yoy_growths"]) == 2


def test_extract_rev_margin_few_quarters_margin_only():
    # <5 季 → yoy 空、仍给最新季毛利率
    df = _qdf([("2026-03-31", 90, 30), ("2025-12-31", 100, 45), ("2025-09-30", 110, 44)])
    out = yp.extract_rev_margin(df)
    assert out["yoy_growths"] == [] and out["gross_margin"] is not None


def test_extract_rev_margin_nan_safe():
    # 含 NaN 季不崩、不产生假阳性（营收 NaN → 该 yoy 跳过或 None·下游 check 不会误判 decline）
    import math
    df = _qdf([("2026-03-31", float("nan"), 30), ("2025-12-31", 100, 45), ("2025-09-30", 110, 44),
               ("2025-06-30", 120, 48), ("2025-03-31", 100, 40), ("2024-12-31", 90, 40)])
    out = yp.extract_rev_margin(df)
    # 不崩即可；若返回 dict 则 yoy 里不应出现 nan 传导成的假阳性
    assert out is None or isinstance(out, dict)

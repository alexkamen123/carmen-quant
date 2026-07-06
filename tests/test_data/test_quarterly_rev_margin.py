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

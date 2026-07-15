import pandas as pd
from finance_agent.data import yf_utils as yp


def _df(rows):
    idx = pd.DatetimeIndex([pd.Timestamp(d, tz="US/Eastern") for d, _, _ in rows],
                           name="Earnings Date")
    return pd.DataFrame(
        {"EPS Estimate": [e for _, e, _ in rows],
         "Reported EPS": [r for _, _, r in rows],
         "Surprise(%)": [0.0 for _ in rows]},
        index=idx,
    )


def test_extract_surprises_sorted_ascending():
    df = _df([("2026-04-30", 1.94, 2.01), ("2025-01-30", 2.34, 2.40)])
    out = yp._extract_surprises(df)
    assert [d.isoformat() for d, _, _ in out] == ["2025-01-30", "2026-04-30"]
    assert out[-1][1] == 2.01 and out[-1][2] == 1.94


def test_extract_surprises_drops_nan_rows():
    df = _df([("2026-07-30", 1.89, float("nan")), ("2026-04-30", 1.94, 2.01)])
    out = yp._extract_surprises(df)
    assert len(out) == 1 and out[0][0].isoformat() == "2026-04-30"


def test_extract_surprises_empty_or_none():
    assert yp._extract_surprises(None) == []
    assert yp._extract_surprises(pd.DataFrame()) == []

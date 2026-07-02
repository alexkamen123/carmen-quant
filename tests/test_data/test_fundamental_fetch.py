# tests/test_data/test_fundamental_fetch.py
"""P1a 因子取数：_extract_close 必须处理 yfinance 1.3.0 的 MultiIndex 列
（否则 df['Close'] 是 DataFrame、.tolist() 抛异常被吞 → 动量因子静默失效）。脱网单测。"""
import pandas as pd

from finance_agent.data import yfinance_provider as yp


def test_extract_close_flat():
    """扁平列(旧版/单层)。"""
    df = pd.DataFrame({"Open": [1.0, 2.0], "Close": [10.0, 20.0]})
    assert yp._extract_close(df) == [10.0, 20.0]


def test_extract_close_multiindex():
    """yfinance 1.3.0 单票 MultiIndex 列 ('Close','AAPL') → 仍取出收盘序列。"""
    cols = pd.MultiIndex.from_tuples([("Close", "AAPL"), ("Open", "AAPL")])
    df = pd.DataFrame([[10.0, 1.0], [20.0, 2.0]], columns=cols)
    assert yp._extract_close(df) == [10.0, 20.0]


def test_extract_close_empty_or_missing():
    assert yp._extract_close(pd.DataFrame()) is None
    assert yp._extract_close(pd.DataFrame({"Open": [1.0]})) is None   # 无 Close 列
    assert yp._extract_close(None) is None


def test_extract_close_drops_nan():
    df = pd.DataFrame({"Close": [10.0, None, 20.0]})
    assert yp._extract_close(df) == [10.0, 20.0]

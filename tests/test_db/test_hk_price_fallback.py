"""港股取价 AkShare 兜底（yfinance 对港股新股常返回空，如 MiniMax 00100）。"""
import pandas as pd
from finance_agent.db import tracker


class _EmptyTicker:
    def __init__(self, *a, **k): pass
    def history(self, *a, **k):
        return pd.DataFrame()           # yfinance 拿不到


def test_hk_falls_back_to_akshare(monkeypatch):
    monkeypatch.setattr(tracker.yf, "Ticker", _EmptyTicker)
    monkeypatch.setattr(tracker, "_hk_price_akshare", lambda t: 576.0)
    assert tracker._fetch_current_price("00100", "hk") == 576.0


def test_us_does_not_use_akshare(monkeypatch):
    monkeypatch.setattr(tracker.yf, "Ticker", _EmptyTicker)
    called = {"hit": False}
    def _boom(t):
        called["hit"] = True
        return 1.0
    monkeypatch.setattr(tracker, "_hk_price_akshare", _boom)
    assert tracker._fetch_current_price("NVDA", "us") is None
    assert called["hit"] is False       # 美股不引入 akshare


def test_yfinance_success_skips_akshare(monkeypatch):
    class _GoodTicker:
        def __init__(self, *a, **k): pass
        def history(self, *a, **k):
            return pd.DataFrame({"Close": [500.0, 533.5]})
    monkeypatch.setattr(tracker.yf, "Ticker", _GoodTicker)
    monkeypatch.setattr(tracker, "_hk_price_akshare",
                        lambda t: (_ for _ in ()).throw(AssertionError("不该调 akshare")))
    assert tracker._fetch_current_price("00700", "hk") == 533.5

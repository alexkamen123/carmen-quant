import pandas as pd
from .base import DataProvider
from .yfinance_provider import YFinanceProvider
from .akshare_provider import AkShareProvider

class DataRouter:
    """根据市场类型路由到对应数据源"""

    def __init__(self):
        self._providers: dict[str, DataProvider] = {
            "us": YFinanceProvider(),
            "hk": AkShareProvider(),
            "cn": AkShareProvider(),
        }

    def get_provider(self, ticker: str, market: str) -> DataProvider:
        if market not in self._providers:
            raise ValueError(f"Unsupported market: {market}. Use: us, hk, cn")
        return self._providers[market]

    async def fetch_ohlcv(self, ticker: str, market: str, days: int = 60) -> pd.DataFrame:
        provider = self.get_provider(ticker, market)
        return await provider.fetch_ohlcv(ticker, days)

    async def fetch_news(self, ticker: str, market: str, limit: int = 5) -> list[dict]:
        provider = self.get_provider(ticker, market)
        return await provider.fetch_news(ticker, limit)

    async def fetch_earnings(self, ticker: str, market: str) -> dict:
        provider = self.get_provider(ticker, market)
        return await provider.fetch_earnings(ticker)

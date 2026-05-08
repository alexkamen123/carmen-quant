from abc import ABC, abstractmethod
import pandas as pd

class DataProvider(ABC):
    """所有数据源的抽象接口"""

    @abstractmethod
    async def fetch_ohlcv(self, ticker: str, days: int = 60) -> pd.DataFrame:
        """
        返回 OHLCV DataFrame，列名统一为：
        open, high, low, close, volume，index 为 DatetimeIndex
        """
        ...

    @abstractmethod
    async def fetch_news(self, ticker: str, limit: int = 5) -> list[dict]:
        """
        返回最近新闻列表，每条：
        {"title": str, "summary": str, "published": str}
        """
        ...

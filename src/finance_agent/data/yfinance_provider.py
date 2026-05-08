import asyncio
from datetime import datetime, timedelta
import pandas as pd
import yfinance as yf
from .base import DataProvider

class YFinanceProvider(DataProvider):
    """美股数据，使用 yfinance（免费）"""

    async def fetch_ohlcv(self, ticker: str, days: int = 60) -> pd.DataFrame:
        end = datetime.today()
        start = end - timedelta(days=days + 10)

        loop = asyncio.get_event_loop()
        df = await loop.run_in_executor(
            None,
            lambda: yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
        )

        if df.empty:
            raise ValueError(f"No data returned for {ticker}")

        # 统一列名为小写，处理 MultiIndex
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0].lower() for c in df.columns]
        else:
            df.columns = [c.lower() for c in df.columns]

        df.index = pd.to_datetime(df.index)
        return df[["open", "high", "low", "close", "volume"]].tail(days)

    async def fetch_news(self, ticker: str, limit: int = 5) -> list[dict]:
        loop = asyncio.get_event_loop()
        stock = await loop.run_in_executor(None, lambda: yf.Ticker(ticker))
        news_raw = getattr(stock, "news", None) or []

        result = []
        for item in news_raw[:limit]:
            result.append({
                "title": item.get("title", ""),
                "summary": item.get("summary", item.get("title", "")),
                "published": str(item.get("providerPublishTime", "")),
            })
        return result

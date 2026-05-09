import asyncio
from datetime import datetime, timedelta
import pandas as pd
import akshare as ak
from .base import DataProvider

class AkShareProvider(DataProvider):
    """港股/A股数据，使用 akshare（免费）"""

    async def fetch_ohlcv(self, ticker: str, days: int = 60) -> pd.DataFrame:
        end = datetime.today().strftime("%Y%m%d")
        start = (datetime.today() - timedelta(days=days + 10)).strftime("%Y%m%d")

        loop = asyncio.get_event_loop()

        if ticker.isdigit() and len(ticker) <= 6:
            # 港股
            df = await loop.run_in_executor(
                None,
                lambda: ak.stock_hk_hist(
                    symbol=ticker, period="daily",
                    start_date=start, end_date=end, adjust="qfq"
                )
            )
            rename = {"日期": "date", "开盘": "open", "最高": "high",
                      "最低": "low", "收盘": "close", "成交量": "volume"}
        else:
            # A股
            df = await loop.run_in_executor(
                None,
                lambda: ak.stock_zh_a_hist(
                    symbol=ticker, period="daily",
                    start_date=start, end_date=end, adjust="qfq"
                )
            )
            rename = {"日期": "date", "开盘": "open", "最高": "high",
                      "最低": "low", "收盘": "close", "成交量": "volume"}

        df = df.rename(columns=rename)
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        return df[["open", "high", "low", "close", "volume"]].tail(days)

    async def fetch_news(self, ticker: str, limit: int = 5) -> list[dict]:
        # 简化版：港股新闻获取，失败时返回空列表不影响主流程
        return []

    async def fetch_earnings(self, ticker: str) -> dict:
        """港股/A股基本面数据（暂返回空，后续可接 akshare 财务接口）"""
        return {}

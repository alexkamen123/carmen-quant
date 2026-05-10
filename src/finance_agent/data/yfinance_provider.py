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

    async def fetch_earnings(self, ticker: str) -> dict:
        """获取基本面数据：营收增速、毛利率、PE、PS、负债率、下次财报日期"""
        loop = asyncio.get_event_loop()
        stock = await loop.run_in_executor(None, lambda: yf.Ticker(ticker))

        info = {}
        try:
            info = await loop.run_in_executor(None, lambda: stock.info) or {}
        except Exception:
            pass

        def safe(key, divisor=1):
            v = info.get(key)
            if v is None:
                return None
            try:
                return round(float(v) / divisor, 2)
            except (TypeError, ValueError):
                return None

        # 营收同比增速（yfinance 提供 revenueGrowth，已是小数如 0.15）
        rev_growth = safe("revenueGrowth")
        if rev_growth is not None:
            rev_growth = round(rev_growth * 100, 1)  # 转成百分比

        # 下次财报日期（取最近一个未来日期）
        next_earnings_date = ""
        try:
            calendar = await loop.run_in_executor(None, lambda: stock.calendar)
            # calendar 可能是 dict{"Earnings Date": [Timestamp, ...]} 或 DataFrame
            if isinstance(calendar, dict):
                dates = calendar.get("Earnings Date", [])
            elif hasattr(calendar, "loc"):          # DataFrame 格式（旧版 yfinance）
                dates = list(calendar.loc["Earnings Date"]) if "Earnings Date" in calendar.index else []
            else:
                dates = []
            today = datetime.today().date()
            future = sorted(
                [d.date() if hasattr(d, "date") else d for d in dates if d is not None],
            )
            future = [d for d in future if d >= today]
            if future:
                next_earnings_date = str(future[0])
        except Exception:
            pass

        return {
            "revenue_growth_yoy": rev_growth,
            "gross_margin": round(safe("grossMargins", 0.01), 1) if safe("grossMargins") is not None else None,
            "pe_ratio": safe("trailingPE"),
            "ps_ratio": safe("priceToSalesTrailing12Months"),
            "debt_to_equity": safe("debtToEquity"),
            "next_earnings_date": next_earnings_date,
        }

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

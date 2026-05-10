import asyncio
import os
from datetime import datetime, timedelta, date as date_type
import pandas as pd
import akshare as ak
import yfinance as yf
from .base import DataProvider

# AkShare 数据来自东方财富，本地开发走代理时 eastmoney.com 可能被拦截。
# 将其加入 NO_PROXY，让 requests 直连；CI 环境无代理，这里是 no-op。
_AKSHARE_NO_PROXY = "eastmoney.com,push2his.eastmoney.com,datacenter-web.eastmoney.com"
_current = os.environ.get("NO_PROXY", "")
if "eastmoney.com" not in _current:
    os.environ["NO_PROXY"] = f"{_current},{_AKSHARE_NO_PROXY}".strip(",")


def _to_yf_hk(ticker: str) -> str:
    """将 AkShare 港股代码转为 yfinance 格式：'00700' → '0700.HK'"""
    return f"{int(ticker):04d}.HK"

class AkShareProvider(DataProvider):
    """港股/A股数据，使用 akshare（免费）"""

    async def fetch_ohlcv(self, ticker: str, days: int = 60) -> pd.DataFrame:
        end = datetime.today().strftime("%Y%m%d")
        start = (datetime.today() - timedelta(days=days + 10)).strftime("%Y%m%d")

        loop = asyncio.get_event_loop()

        if ticker.isdigit() and len(ticker) <= 6:
            # 港股：优先 AkShare，失败自动降级到 yfinance
            try:
                df = await loop.run_in_executor(
                    None,
                    lambda: ak.stock_hk_hist(
                        symbol=ticker, period="daily",
                        start_date=start, end_date=end, adjust="qfq"
                    )
                )
                rename = {"日期": "date", "开盘": "open", "最高": "high",
                          "最低": "low", "收盘": "close", "成交量": "volume"}
                df = df.rename(columns=rename)
                df["date"] = pd.to_datetime(df["date"])
                df = df.set_index("date").sort_index()
                result = df[["open", "high", "low", "close", "volume"]].tail(days)
                if len(result) >= 20:
                    return result
                raise ValueError(f"AkShare 数据不足 {len(result)} 条")
            except Exception as e:
                print(f"[AkShare] {ticker} 降级到 yfinance: {e}")
                # 降级：yfinance 港股格式 0700.HK
                yf_ticker = _to_yf_hk(ticker)
                df_yf = await loop.run_in_executor(
                    None,
                    lambda: yf.download(yf_ticker, period=f"{days + 10}d",
                                        progress=False, auto_adjust=True)
                )
                if df_yf.empty:
                    raise ValueError(f"yfinance 也无法获取 {ticker} 数据")
                if isinstance(df_yf.columns, pd.MultiIndex):
                    df_yf.columns = [c[0].lower() for c in df_yf.columns]
                else:
                    df_yf.columns = [c.lower() for c in df_yf.columns]
                df_yf.index = pd.to_datetime(df_yf.index)
                return df_yf[["open", "high", "low", "close", "volume"]].tail(days)
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
        """
        港股基本面数据：via yfinance（0700.HK 格式）。
        A股代码格式不符，暂返回空；yfinance 查不到时静默返回空。
        """
        # A 股代码含字母或超过 6 位时跳过（yfinance 港股格式只适用数字代码）
        if not ticker.isdigit():
            return {}

        yf_ticker = _to_yf_hk(ticker)
        loop = asyncio.get_event_loop()
        try:
            stock = await loop.run_in_executor(None, lambda: yf.Ticker(yf_ticker))
            info = await loop.run_in_executor(None, lambda: stock.info) or {}
        except Exception:
            return {}

        def safe(key, divisor=1):
            v = info.get(key)
            if v is None:
                return None
            try:
                return round(float(v) / divisor, 2)
            except (TypeError, ValueError):
                return None

        rev_growth = safe("revenueGrowth")
        if rev_growth is not None:
            rev_growth = round(rev_growth * 100, 1)

        gross_margins = safe("grossMargins")
        gross_margin = round(gross_margins * 100, 1) if gross_margins is not None else None

        # 下次财报日期（港股同样走 yfinance calendar）
        next_earnings_date = ""
        try:
            calendar = await loop.run_in_executor(None, lambda: stock.calendar)
            if isinstance(calendar, dict):
                dates = calendar.get("Earnings Date", [])
            elif hasattr(calendar, "loc"):
                dates = list(calendar.loc["Earnings Date"]) if "Earnings Date" in calendar.index else []
            else:
                dates = []
            today = datetime.today().date()
            future = sorted([d.date() if hasattr(d, "date") else d for d in dates if d is not None])
            future = [d for d in future if d >= today]
            if future:
                next_earnings_date = str(future[0])
        except Exception:
            pass

        return {
            "revenue_growth_yoy": rev_growth,
            "gross_margin": gross_margin,
            "pe_ratio": safe("trailingPE"),
            "ps_ratio": safe("priceToSalesTrailing12Months"),
            "debt_to_equity": safe("debtToEquity"),
            "next_earnings_date": next_earnings_date,
        }

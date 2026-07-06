import asyncio
from datetime import datetime, timedelta
import pandas as pd
import yfinance as yf
from .base import DataProvider
from .yf_utils import _YF_SEM, _download_with_retry, _ticker_info_with_retry, _ticker_calendar_with_retry


class YFinanceProvider(DataProvider):
    """美股数据，使用 yfinance（免费）"""

    async def fetch_ohlcv(self, ticker: str, days: int = 60) -> pd.DataFrame:
        end = datetime.today()
        start = end - timedelta(days=days + 10)
        loop = asyncio.get_event_loop()
        async with _YF_SEM:
            df = await loop.run_in_executor(
                None,
                lambda: _download_with_retry(ticker, start=start, end=end)
            )
        if df.empty:
            raise ValueError(f"No data returned for {ticker}")
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0].lower() for c in df.columns]
        else:
            df.columns = [c.lower() for c in df.columns]
        df.index = pd.to_datetime(df.index)
        return df[["open", "high", "low", "close", "volume"]].tail(days)

    async def fetch_earnings(self, ticker: str) -> dict:
        """获取基本面数据：营收增速、毛利率、PE、PS、负债率、下次财报日期"""
        loop = asyncio.get_event_loop()

        async with _YF_SEM:
            info = await loop.run_in_executor(None, lambda: _ticker_info_with_retry(ticker))

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

        next_earnings_date = ""
        try:
            async with _YF_SEM:
                calendar = await loop.run_in_executor(
                    None, lambda: _ticker_calendar_with_retry(ticker)
                )
            if isinstance(calendar, dict):
                dates = calendar.get("Earnings Date", [])
            elif hasattr(calendar, "loc"):
                dates = list(calendar.loc["Earnings Date"]) if "Earnings Date" in calendar.index else []
            else:
                dates = []
            today = datetime.today().date()
            future = sorted(
                [d.date() if hasattr(d, "date") else d for d in dates if d is not None]
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
        async with _YF_SEM:
            news_raw = await loop.run_in_executor(
                None, lambda: getattr(yf.Ticker(ticker), "news", None) or []
            )
        result = []
        for item in news_raw[:limit]:
            result.append({
                "title": item.get("title", ""),
                "summary": item.get("summary", item.get("title", "")),
                "published": str(item.get("providerPublishTime", "")),
            })
        return result


# ── P1a 基本面因子原始数据（flag-gated·仅美股·防御式取数）───────────────────────
def _latest_col(df) -> dict | None:
    """取财务/资产负债表最近一列 → {行名: 值}（NaN→None）。空/异常→None。"""
    try:
        if df is None or df.empty:
            return None
        return {k: (None if pd.isna(v) else float(v)) for k, v in df.iloc[:, 0].items()}
    except Exception:
        return None


def _extract_close(df) -> list | None:
    """从日线 DataFrame 取收盘 list，兼容 yfinance 1.3.0 的 MultiIndex 列
    （单票也返回 ('Close',ticker)；不拍平则 df['Close'] 是 DataFrame、.tolist() 抛异常）。
    空/无 Close 列 → None。对齐 fetch_ohlcv 的 MultiIndex 处理。"""
    if df is None or getattr(df, "empty", True):
        return None
    d = df.copy()
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = [c[0].lower() for c in d.columns]
    else:
        d.columns = [str(c).lower() for c in d.columns]
    if "close" not in d.columns:
        return None
    close = d["close"]
    if getattr(close, "ndim", 1) > 1:   # 多票时同名列兜底取首列（本用途仅单票）
        close = close.iloc[:, 0]
    return [float(x) for x in close.dropna().tolist()]


def _close_series(ticker: str, days: int = 400) -> list | None:
    """取 ~13 个月日线收盘 list（供 12-1 月动量，独立于 60 天技术面窗口）。
    走 _download_with_retry（复用已验证取数路径+重试）。异常→None。"""
    try:
        end = datetime.today()
        start = end - timedelta(days=days)
        return _extract_close(_download_with_retry(ticker, start=start, end=end))
    except Exception:
        return None


async def fetch_fundamental_factors(ticker: str):
    """取四因子原始数据并合成 FundamentalFactors。全程防御式，任一子取数失败该因子降级 None，
    绝不抛异常拖垮日报。仅在 fundamental_factors flag 开时由 fundamentals_node 调用。"""
    from finance_agent.signals.fundamental_score import calculate_fundamental_factors
    loop = asyncio.get_event_loop()
    async with _YF_SEM:
        info = await loop.run_in_executor(None, lambda: _ticker_info_with_retry(ticker))
    fin_col = bal_col = close = None
    try:
        async with _YF_SEM:
            fin_col = await loop.run_in_executor(None, lambda: _latest_col(yf.Ticker(ticker).financials))
            bal_col = await loop.run_in_executor(None, lambda: _latest_col(yf.Ticker(ticker).balance_sheet))
    except Exception:
        pass
    try:
        async with _YF_SEM:
            close = await loop.run_in_executor(None, lambda: _close_series(ticker))
    except Exception:
        pass
    return calculate_fundamental_factors(info or {}, fin_col, bal_col, close)


def extract_rev_margin(df):
    """从 quarterly_financials DataFrame 抽最近若干季同比营收增速 + 最新季毛利率。

    df 列=季度、行含 'Total Revenue'/'Gross Profit'。
    返回 {"yoy_growths": [...], "gross_margin": float} 或 None（数据不足/缺行）。
    yoy = (本季营收 − 去年同季营收)/|去年同季| ·%；至少 5 季才能算最近 2 季同比。
    """
    if df is None or getattr(df, "empty", True):
        return None
    try:
        if "Total Revenue" not in df.index:
            return None
        cols_sorted = sorted(df.columns)          # 升序：老→新
        if len(cols_sorted) < 5:
            # 不足算同比，但仍可给最新季毛利率
            if "Gross Profit" in df.index:
                latest = cols_sorted[-1]
                r0 = df.at["Total Revenue", latest]
                g0 = df.at["Gross Profit", latest]
                gm = float(g0) / float(r0) * 100 if r0 else None
                return {"yoy_growths": [], "gross_margin": gm} if gm is not None else None
            return None
        seq = [float(df.at["Total Revenue", c]) for c in cols_sorted]
        yoy = []
        for i in range(4, len(seq)):
            base = seq[i - 4]
            if base:
                yoy.append(round((seq[i] - base) / abs(base) * 100, 2))
        latest = cols_sorted[-1]
        r0 = df.at["Total Revenue", latest]
        g0 = df.at["Gross Profit", latest] if "Gross Profit" in df.index else None
        gm = float(g0) / float(r0) * 100 if (g0 is not None and r0) else None
        return {"yoy_growths": yoy, "gross_margin": gm}
    except Exception:
        return None


def fetch_quarterly_rev_margin(ticker):
    """同步取 yf.Ticker(t).quarterly_financials → extract_rev_margin。失败静默 None。

    仿 fetch_fundamental_factors：由调用方用 run_in_executor 包装。
    ⚠️ quarterly_financials 行名跨 yfinance 版本可能不同，若线上验证发现字段不符，
    需调整或改用 .quarterly_income_stmt。
    """
    try:
        return extract_rev_margin(yf.Ticker(ticker).quarterly_financials)
    except Exception:
        return None

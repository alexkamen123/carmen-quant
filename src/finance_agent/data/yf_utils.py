# src/finance_agent/data/yf_utils.py
"""
共享的 yfinance 并发保护工具。

Yahoo Finance API 使用 crumb 做会话认证，高并发下多个 yf.download() /
yf.Ticker().info 会互相踩踏 crumb 导致 401。
_YF_SEM 全局限制同时最多 2 个 yfinance 请求；_download_with_retry 在失败时
最多重试 3 次（间隔 2s），覆盖偶发的 crumb 竞争。
"""
import asyncio
import contextlib
import os
import time
import yfinance as yf

# 全局 Semaphore — 限制同时最多 2 个 yfinance API 请求
_YF_SEM = asyncio.Semaphore(2)

# 全局 Semaphore — AkShare / 东方财富并发能力弱，限制为串行
_AK_SEM = asyncio.Semaphore(1)


def _download_with_retry(ticker: str, retries: int = 3, **kwargs) -> "pd.DataFrame":
    """
    同步下载，失败时最多重试 retries 次（每次间隔 2s）。
    kwargs 透传给 yf.download()。
    """
    import pandas as pd
    last_err = None
    for attempt in range(retries):
        try:
            df = yf.download(ticker, progress=False, auto_adjust=True, **kwargs)
            if not df.empty:
                return df
        except Exception as e:
            last_err = e
        if attempt < retries - 1:
            time.sleep(2)
    raise ValueError(
        f"No data returned for {ticker}" + (f" ({last_err})" if last_err else "")
    )


def _ticker_info_with_retry(ticker: str, retries: int = 3) -> dict:
    """
    同步获取 yf.Ticker(ticker).info，失败时最多重试 retries 次。
    """
    last_err = None
    for attempt in range(retries):
        try:
            info = yf.Ticker(ticker).info or {}
            if info:
                return info
        except Exception as e:
            last_err = e
        if attempt < retries - 1:
            time.sleep(2)
    return {}


def _ticker_calendar_with_retry(ticker: str, retries: int = 3):
    """
    同步获取 yf.Ticker(ticker).calendar，失败时静默返回 None。
    """
    for attempt in range(retries):
        try:
            return yf.Ticker(ticker).calendar
        except Exception:
            if attempt < retries - 1:
                time.sleep(2)
    return None


@contextlib.contextmanager
def _direct_connection():
    """
    临时摘掉 HTTP_PROXY/HTTPS_PROXY，让 requests 直连目标服务器（适用于 AkShare/东方财富）。
    退出时自动恢复原值。
    """
    saved = {k: os.environ.pop(k, None) for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")}
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v

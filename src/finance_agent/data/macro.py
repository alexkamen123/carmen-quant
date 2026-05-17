# src/finance_agent/data/macro.py
"""
拉取宏观市场背景数据（VIX、大盘指数涨跌）。
全部走 yfinance，无需额外 API key。
"""
import asyncio
from dataclasses import dataclass
import yfinance as yf
from .yf_utils import _YF_SEM


@dataclass
class MacroContext:
    vix: float           # VIX 恐慌指数（当前值）
    vix_level: str       # 低(<20) / 中(20-30) / 高(>30)
    spx_chg: float       # 标普500 当日涨跌 %
    nasdaq_chg: float    # 纳斯达克 当日涨跌 %
    hsi_chg: float       # 恒生指数 当日涨跌 %

    def to_prompt_str(self) -> str:
        def fmt(v: float) -> str:
            return f"{'+' if v >= 0 else ''}{v:.2f}%"
        return (
            f"VIX恐慌指数：{self.vix:.1f}（{self.vix_level}）｜"
            f"标普500：{fmt(self.spx_chg)}｜"
            f"纳斯达克：{fmt(self.nasdaq_chg)}｜"
            f"恒生指数：{fmt(self.hsi_chg)}"
        )


def _day_change(ticker_symbol: str) -> float:
    """返回当日涨跌幅 %，失败返回 0.0"""
    try:
        t = yf.Ticker(ticker_symbol)
        hist = t.history(period="2d")
        if len(hist) < 2:
            return 0.0
        prev, last = float(hist["Close"].iloc[-2]), float(hist["Close"].iloc[-1])
        return (last - prev) / prev * 100
    except Exception:
        return 0.0


def _vix_value() -> float:
    try:
        t = yf.Ticker("^VIX")
        hist = t.history(period="1d")
        return float(hist["Close"].iloc[-1]) if len(hist) > 0 else 20.0
    except Exception:
        return 20.0


async def fetch_macro_context() -> MacroContext:
    """异步包装（在 executor 中运行 yfinance 同步调用，共享 _YF_SEM 避免 crumb 竞争）"""
    loop = asyncio.get_event_loop()

    async def _sem_vix():
        async with _YF_SEM:
            return await loop.run_in_executor(None, _vix_value)

    async def _sem_chg(sym: str):
        async with _YF_SEM:
            return await loop.run_in_executor(None, _day_change, sym)

    vix, spx_chg, nasdaq_chg, hsi_chg = await asyncio.gather(
        _sem_vix(),
        _sem_chg("^GSPC"),
        _sem_chg("^IXIC"),
        _sem_chg("^HSI"),
    )

    if vix < 20:
        level = "低（市场平静）"
    elif vix < 30:
        level = "中（波动偏高）"
    else:
        level = "高（市场恐慌）"

    return MacroContext(
        vix=vix,
        vix_level=level,
        spx_chg=spx_chg,
        nasdaq_chg=nasdaq_chg,
        hsi_chg=hsi_chg,
    )

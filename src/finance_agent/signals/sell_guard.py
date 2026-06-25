# src/finance_agent/signals/sell_guard.py
"""
卖飞守门：当系统给出减仓/卖出建议、但技术面仍强势上行时，追加警示行。
不改变建议本身，只产出一行提醒文案。

历史数据背景：卖侧 alpha = −7.37%（n=10），减仓建议卖飞 6/10，平均跑输大盘 7.4%。
"""

from __future__ import annotations


# bearish 建议关键词
_BEARISH_RECOMMENDATIONS = {"减仓", "卖出"}


def flag_sell_into_strength(
    recommendation: str,
    position_change: str,
    rsi: float | None,
    price: float | None,
    ma20: float | None,
    ma60: float | None,
    macd: float | None,
) -> str | None:
    """
    守门函数：识别「bearish 建议 + 技术面仍强势上行」并产出警示文案。

    触发条件（必须同时满足）：
      1. bearish 建议：recommendation ∈ {"减仓","卖出"} 或 position_change 以"减仓"开头
      2. 技术面强势上行（非超买）：
         - rsi < 70（非超买区，真实强势）
         - price > ma20 > ma60（均线多头排列，趋势向上）
         - macd > 0（MACD 值为正，动量持续）

    任一技术指标为 None → graceful 返回 None（数据不全时不瞎警示）
    非 bearish → None
    技术面走弱/超买 → None

    Returns:
        str: 警示文案（含"⚠️ 卖飞风险"），或 None（不触发）
    """
    # 判断是否为 bearish 建议
    is_bearish = (
        recommendation in _BEARISH_RECOMMENDATIONS
        or (position_change or "").startswith("减仓")
    )
    if not is_bearish:
        return None

    # 任一技术指标缺失 → graceful 跳过
    if any(v is None for v in (rsi, price, ma20, ma60, macd)):
        return None

    # 技术面强势条件：全部满足才触发
    is_technically_strong = (
        rsi < 70           # 非超买（超买时回调反而合理，不属于"卖飞"风险）
        and price > ma20   # 价格在 MA20 上方
        and ma20 > ma60    # MA20 在 MA60 上方（均线多头排列）
        and macd > 0       # MACD 值为正（动量向上）
    )
    if not is_technically_strong:
        return None

    # 产出警示文案
    return (
        "⚠️ **卖飞风险**：技术面仍强势（RSI未超买·均线多头排列·MACD正值），"
        "历史上减仓建议卖飞 6/10、平均跑输大盘 7.4%——"
        "请确认离场理由是基本面恶化或止损触发，而非单纯价格上涨"
    )

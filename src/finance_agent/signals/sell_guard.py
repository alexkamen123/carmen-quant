# src/finance_agent/signals/sell_guard.py
"""
卖飞守门：当系统给出减仓/卖出建议、但技术面仍强势上行时，追加警示行。
不改变建议本身，只产出一行提醒文案。

历史数据背景：卖侧 alpha = −7.37%（n=10），减仓建议卖飞 6/10，平均跑输大盘 7.4%。
"""

from __future__ import annotations


# bearish 建议关键词
_BEARISH_RECOMMENDATIONS = {"减仓", "卖出"}

# hold 立场关键词（买入/减仓/卖出 以外均视为持有立场）
_NON_HOLD_RECOMMENDATIONS = {"买入", "减仓", "卖出"}


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


def flag_hold_into_weakness(
    recommendation: str,
    rsi: float | None,
    price: float | None,
    ma20: float | None,
    ma60: float | None,
    macd: float | None,
) -> str | None:
    """
    守门函数：识别「持有立场 + 技术面明显走弱」并产出警示文案。

    触发条件（必须同时满足）：
      1. 持有立场：recommendation 不是"买入"/"减仓"/"卖出"
         （即"持有"/"观望"等明确方向以外的立场都算持有）
      2. 技术面走弱：
         - rsi < 50（非超卖但偏弱，动量转负）
         - price < ma20 < ma60（均线空头排列，趋势向下）
         - macd < 0（MACD 值为负，动量持续向下）

    任一技术指标为 None → graceful 返回 None（数据不全时不瞎警示）
    非持有立场（买入/减仓/卖出）→ None
    技术面不弱 → None
    严格边界：rsi==50 / price==ma20 / ma20==ma60 / macd==0 均不触发

    历史背景：审计发现 7 例「该减没减」，对技术面已走弱的票仍持有/维持，
    结果显著跑输大盘——本守门函数专为识别此类风险而生。

    Returns:
        str: 警示文案（含"⚠️ 该减没减风险"），或 None（不触发）
    """
    # 判断是否为持有立场（买入/减仓/卖出 明确方向 → 非持有）
    is_hold = recommendation not in _NON_HOLD_RECOMMENDATIONS
    if not is_hold:
        return None

    # 任一技术指标缺失 → graceful 跳过
    if any(v is None for v in (rsi, price, ma20, ma60, macd)):
        return None

    # 技术面走弱条件：全部满足才触发（严格 < 边界）
    is_technically_weak = (
        rsi < 50         # RSI 偏弱（严格小于 50）
        and price < ma20  # 价格在 MA20 下方
        and ma20 < ma60   # MA20 在 MA60 下方（均线空头排列）
        and macd < 0      # MACD 值为负（动量向下）
    )
    if not is_technically_weak:
        return None

    # 产出警示文案
    return (
        "⚠️ **该减没减风险**：技术面已明显走弱（RSI偏弱·均线空头排列·MACD负值），"
        "历史上持有判断错7例·对技术面走弱的票仍持有致显著跑输——"
        "请确认继续持有的基本面理由，或考虑止损/减仓"
    )

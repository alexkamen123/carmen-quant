# src/finance_agent/agents/portfolio_manager.py
import json
import anthropic
import os
from finance_agent.graph.state import StockAnalysis
from finance_agent.agents.prompts import PM_SYSTEM, PM_USER

MARKET_LABEL = {"us": "美股", "hk": "港股", "cn": "A股"}


async def run_portfolio_manager(analysis: StockAnalysis) -> StockAnalysis:
    """使用 Claude 做最终裁决（复杂推理）"""
    client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    user_msg = PM_USER.format(
        ticker=analysis.ticker,
        market=MARKET_LABEL.get(analysis.market, analysis.market),
        signals_str=analysis.signals.to_prompt_str(),
        bull_thesis=analysis.bull_thesis,
        bear_thesis=analysis.bear_thesis,
    )

    message = await client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=500,
        system=PM_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )

    raw = message.content[0].text.strip()
    # 提取 JSON（Claude 有时会在 JSON 前后加说明文字）
    start = raw.find("{")
    end = raw.rfind("}") + 1
    decision = json.loads(raw[start:end])

    return analysis.model_copy(update={
        "recommendation": decision.get("recommendation", "观望"),
        "confidence":     decision.get("confidence", "低"),
        "entry_hint":     decision.get("entry_hint", ""),
        "key_risk":       decision.get("key_risk", ""),
        "one_line":       decision.get("one_line", ""),
    })

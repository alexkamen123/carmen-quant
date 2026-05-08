# src/finance_agent/agents/bear_agent.py
from finance_agent.agents.bull_agent import deepseek_chat  # 复用同一函数
from finance_agent.graph.state import StockAnalysis
from finance_agent.agents.prompts import BEAR_SYSTEM, BEAR_USER


async def run_bear_analysis(analysis: StockAnalysis) -> StockAnalysis:
    news_str = "\n".join(
        f"- {n.title}" for n in analysis.news
    ) or "暂无新闻"

    user_msg = BEAR_USER.format(
        signals_str=analysis.signals.to_prompt_str(),
        news_str=news_str,
    )
    thesis = await deepseek_chat(BEAR_SYSTEM, user_msg)
    return analysis.model_copy(update={"bear_thesis": thesis})

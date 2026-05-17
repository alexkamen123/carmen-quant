# src/finance_agent/agents/bull_agent.py
from openai import AsyncOpenAI
from finance_agent.graph.state import StockAnalysis
from finance_agent.agents.prompts import BULL_SYSTEM, BULL_USER
import os


async def deepseek_chat(system: str, user: str) -> str:
    """调用 DeepSeek API（OpenAI 兼容接口）"""
    client = AsyncOpenAI(
        api_key=os.environ["DEEPSEEK_API_KEY"],
        base_url="https://api.deepseek.com",
        timeout=90.0,
    )
    response = await client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.3,
        max_tokens=400,
    )
    return response.choices[0].message.content.strip()


async def run_bull_analysis(analysis: StockAnalysis) -> StockAnalysis:
    news_str = "\n".join(f"- {n.title}" for n in analysis.news) or "暂无新闻"
    peer_news_str = (
        "\n".join(f"- {n.title}" for n in analysis.peer_news)
        if analysis.peer_news else ""
    )
    if peer_news_str:
        news_str += f"\n\n【竞争对手动态】\n{peer_news_str}"

    fundamental_view = analysis.earnings.fundamental_view or "暂无基本面数据"

    user_msg = BULL_USER.format(
        signals_str=analysis.signals.to_prompt_str(),
        fundamental_view=fundamental_view,
        news_str=news_str,
    )
    thesis = await deepseek_chat(BULL_SYSTEM, user_msg)
    return analysis.model_copy(update={"bull_thesis": thesis})

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
    news_str = "\n".join(
        f"- {n.title}" for n in analysis.news
    ) or "暂无新闻"

    user_msg = BULL_USER.format(
        signals_str=analysis.signals.to_prompt_str(),
        news_str=news_str,
    )
    thesis = await deepseek_chat(BULL_SYSTEM, user_msg)
    # 返回更新后的对象（Pydantic immutable，用 model_copy）
    return analysis.model_copy(update={"bull_thesis": thesis})

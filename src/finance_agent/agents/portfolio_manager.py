# src/finance_agent/agents/portfolio_manager.py
import json
import os
import anthropic
from finance_agent.graph.state import StockAnalysis
from finance_agent.agents.prompts import PM_SYSTEM, PM_USER
from finance_agent.agents.bull_agent import deepseek_chat

MARKET_LABEL = {"us": "美股", "hk": "港股", "cn": "A股"}


async def _claude_sdk_chat(system: str, user: str) -> str:
    """
    通过 anthropic SDK 调用 Claude。
    - ANTHROPIC_API_KEY 存在时：标准 API 认证
    - CLAUDE_CODE_OAUTH_TOKEN 存在时：用 OAuth Bearer token 认证（Pro 订阅）
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")

    if api_key:
        client = anthropic.AsyncAnthropic(api_key=api_key)
    else:
        # OAuth token 作为 Bearer token 传入
        client = anthropic.AsyncAnthropic(auth_token=oauth_token)

    message = await client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=600,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return message.content[0].text.strip()


async def run_portfolio_manager(analysis: StockAnalysis) -> StockAnalysis:
    """
    最终裁决：优先用 Claude SDK（API Key 或 Pro OAuth token），回退到 DeepSeek。
    - 本地/CI：有 CLAUDE_CODE_OAUTH_TOKEN 或 ANTHROPIC_API_KEY 时用 Claude
    - 否则：DeepSeek 兜底
    """
    user_msg = PM_USER.format(
        ticker=analysis.ticker,
        market=MARKET_LABEL.get(analysis.market, analysis.market),
        signals_str=analysis.signals.to_prompt_str(),
        bull_thesis=analysis.bull_thesis,
        bear_thesis=analysis.bear_thesis,
    )

    has_claude_auth = bool(
        os.environ.get("ANTHROPIC_API_KEY") or
        os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    )

    if has_claude_auth:
        try:
            raw = await _claude_sdk_chat(PM_SYSTEM, user_msg)
        except Exception as e:
            print(f"[PM] Claude SDK 失败，降级到 DeepSeek: {e}")
            raw = await deepseek_chat(PM_SYSTEM, user_msg)
    else:
        raw = await deepseek_chat(PM_SYSTEM, user_msg)

    # 提取 JSON（模型有时会在 JSON 前后加说明文字）
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

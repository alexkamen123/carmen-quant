# src/finance_agent/agents/portfolio_manager.py
import asyncio
import json
import os
import shutil
from finance_agent.graph.state import StockAnalysis
from finance_agent.agents.prompts import PM_SYSTEM, PM_USER
from finance_agent.agents.bull_agent import deepseek_chat

MARKET_LABEL = {"us": "美股", "hk": "港股", "cn": "A股"}


async def _claude_cli_chat(system: str, user: str) -> str:
    """
    通过 claude CLI 子进程调用（使用 Pro OAuth，无需 API Key）。
    需要本地已登录（claude setup-token）或 CLAUDE_CODE_OAUTH_TOKEN 环境变量。
    """
    prompt = f"{system}\n\n{user}"
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        lambda: __import__("subprocess").run(
            ["claude", "-p", prompt, "--output-format", "text"],
            capture_output=True, text=True, timeout=60,
        )
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI 调用失败: {result.stderr[:200]}")
    return result.stdout.strip()


async def run_portfolio_manager(analysis: StockAnalysis) -> StockAnalysis:
    """
    最终裁决：优先用 claude CLI（Pro OAuth），回退到 DeepSeek。
    - 本地：claude 已登录即可
    - GitHub Actions：设置 CLAUDE_CODE_OAUTH_TOKEN secret
    - 无 claude CLI 或 token 时：自动降级到 DeepSeek
    """
    user_msg = PM_USER.format(
        ticker=analysis.ticker,
        market=MARKET_LABEL.get(analysis.market, analysis.market),
        signals_str=analysis.signals.to_prompt_str(),
        bull_thesis=analysis.bull_thesis,
        bear_thesis=analysis.bear_thesis,
    )

    # 判断是否可用 claude CLI
    use_claude = shutil.which("claude") is not None and (
        os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or
        os.path.exists(os.path.expanduser("~/.claude/.credentials.json"))
    )

    if use_claude:
        try:
            raw = await _claude_cli_chat(PM_SYSTEM, user_msg)
        except Exception as e:
            print(f"[PM] claude CLI 失败，降级到 DeepSeek: {e}")
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

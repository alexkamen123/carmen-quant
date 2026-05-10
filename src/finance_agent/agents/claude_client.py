# src/finance_agent/agents/claude_client.py
"""
通过 claude -p 子进程调用 Claude（走 Claude Code 路径，Pro 订阅不受 API 限速）。
"""
import asyncio
import os
import re
import subprocess


async def claude_cli_chat(system: str, user: str, timeout: int = 120) -> str:
    """
    异步调用 Claude CLI（claude -p）。
    使用 run_in_executor 包住同步 subprocess.run，避免 event loop 阻塞。
    """
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        lambda: subprocess.run(
            ["claude", "-p", user,
             "--system-prompt", system,
             "--output-format", "text"],
            capture_output=True, text=True, timeout=timeout,
        )
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI exit {result.returncode}: {result.stderr[:200]}")
    return result.stdout.strip()


def has_claude_cli() -> bool:
    """检查当前环境是否有 Claude CLI 和认证信息"""
    return bool(
        os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")
    )


def strip_markdown(text: str) -> str:
    """去掉模型输出的 markdown 代码块包裹"""
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text)
    return text.strip()

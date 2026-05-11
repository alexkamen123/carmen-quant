# src/finance_agent/agents/claude_client.py
"""
通过 claude -p 子进程调用 Claude（走 Claude Code 路径，Pro 订阅不受 API 限速）。
"""
import asyncio
import os
import re
import subprocess


async def claude_cli_chat(system: str, user: str, timeout: int = 120,
                          max_retries: int = 2) -> str:
    """
    异步调用 Claude CLI（claude -p）。
    使用 run_in_executor 包住同步 subprocess.run，避免 event loop 阻塞。
    exit 1 且 stderr 为空时（偶发冷启动/网络抖动）自动重试，最多 max_retries 次。
    """
    loop = asyncio.get_event_loop()

    def _run() -> subprocess.CompletedProcess:
        return subprocess.run(
            ["claude", "-p", user,
             "--system-prompt", system,
             "--output-format", "text"],
            capture_output=True, text=True, timeout=timeout,
        )

    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        result = await loop.run_in_executor(None, _run)
        if result.returncode == 0:
            return result.stdout.strip()
        last_err = RuntimeError(
            f"claude CLI exit {result.returncode}: {result.stderr[:200]}"
        )
        # 有实质性错误信息就直接抛，不再重试（不是偶发抖动）
        if result.stderr.strip():
            raise last_err
        # stderr 为空的偶发失败 → 等一会儿重试
        if attempt < max_retries:
            await asyncio.sleep(3 * (attempt + 1))  # 3s, 6s

    raise last_err  # type: ignore[misc]


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

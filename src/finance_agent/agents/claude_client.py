# src/finance_agent/agents/claude_client.py
"""
Claude 调用层，支持两种后端：
  - SDK 直连（ANTHROPIC_API_KEY）：适合 GitHub Actions / CI 环境，稳定可靠
  - CLI 子进程（CLAUDE_CODE_OAUTH_TOKEN）：适合本地，走 Claude Pro 订阅免 API 费用

优先级：ANTHROPIC_API_KEY > CLAUDE_CODE_OAUTH_TOKEN > 抛异常
外部调用方无需关心后端选择，直接调用 claude_cli_chat / has_claude_cli 即可（保持原有接口）。
"""
import asyncio
import os
import re
import subprocess

import anthropic

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")


async def _claude_sdk_chat(system: str, user: str, timeout: int = 120) -> str:
    """SDK 直连（需要 ANTHROPIC_API_KEY）"""
    client = anthropic.AsyncAnthropic()
    message = await asyncio.wait_for(
        client.messages.create(
            model=CLAUDE_MODEL,
            # 4096：PM 批量 17 只 ×10 字段 ≥2000 tokens，原 1500 会截断 → JSON 解析
            # 失败 → 静默整批降级 DeepSeek（06-12 自检 P1）
            max_tokens=4096,
            system=system,
            messages=[{"role": "user", "content": user}],
        ),
        timeout=float(timeout),
    )
    if getattr(message, "stop_reason", None) == "max_tokens":
        print("[Claude] ⚠️ 输出被 max_tokens 截断，JSON 可能不完整（将触发降级）")
    return message.content[0].text


async def _claude_cli_subprocess(system: str, user: str, timeout: int = 120,
                                 max_retries: int = 2) -> str:
    """CLI 子进程（需要 CLAUDE_CODE_OAUTH_TOKEN）"""
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
        if result.stderr.strip():
            raise last_err
        if attempt < max_retries:
            await asyncio.sleep(3 * (attempt + 1))

    raise last_err  # type: ignore[misc]


async def claude_cli_chat(system: str, user: str, timeout: int = 120,
                          max_retries: int = 2) -> str:
    """
    统一入口（对外接口，名称保持不变）。
    优先 CLI（OAuth Token，Claude Pro 订阅免费用），其次 SDK（Anthropic API Key，付费）。
    """
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return await _claude_cli_subprocess(system, user, timeout=timeout,
                                            max_retries=max_retries)
    if os.environ.get("ANTHROPIC_API_KEY"):
        return await _claude_sdk_chat(system, user, timeout=timeout)
    raise RuntimeError("未配置 CLAUDE_CODE_OAUTH_TOKEN 或 ANTHROPIC_API_KEY，无法调用 Claude")


def has_claude_cli() -> bool:
    """检查当前环境是否能调用 Claude（CLI OAuth 或 SDK API Key 均算）"""
    return bool(
        os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or
        os.environ.get("ANTHROPIC_API_KEY")
    )


async def _claude_cli_websearch(system: str, user: str, timeout: int) -> str:
    """CLI 子进程联网搜索：放开 WebSearch/WebFetch 工具，让 Claude 自行联网取证"""
    loop = asyncio.get_event_loop()

    def _run() -> subprocess.CompletedProcess:
        return subprocess.run(
            ["claude", "-p", user,
             "--system-prompt", system,
             "--allowedTools", "WebSearch,WebFetch",
             "--output-format", "text"],
            capture_output=True, text=True, timeout=timeout,
        )

    result = await loop.run_in_executor(None, _run)
    if result.returncode != 0:
        raise RuntimeError(
            f"claude CLI(websearch) exit {result.returncode}: {result.stderr[:200]}"
        )
    return result.stdout.strip()


async def _claude_sdk_websearch(system: str, user: str, timeout: int,
                                max_tokens: int) -> str:
    """SDK 直连联网搜索：附加 Anthropic 原生 web_search 服务端工具"""
    client = anthropic.AsyncAnthropic()
    message = await asyncio.wait_for(
        client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            tools=[{"type": "web_search_20250305", "name": "web_search",
                    "max_uses": 8}],
        ),
        timeout=float(timeout),
    )
    # 联网搜索响应含多个 content block，只拼接文本块
    texts = [b.text for b in message.content
             if getattr(b, "type", None) == "text"]
    return "\n".join(texts).strip()


async def claude_websearch_chat(system: str, user: str, timeout: int = 300,
                                max_tokens: int = 4000) -> str:
    """
    联网搜索增强的 Claude 调用（对外统一入口）。
    与 claude_cli_chat 同样的后端优先级：CLI（OAuth）> SDK（API Key）。
    区别：放开联网搜索能力，让模型自行获取最新市场信息。
    """
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return await _claude_cli_websearch(system, user, timeout=timeout)
    if os.environ.get("ANTHROPIC_API_KEY"):
        return await _claude_sdk_websearch(system, user, timeout=timeout,
                                           max_tokens=max_tokens)
    raise RuntimeError("未配置 CLAUDE_CODE_OAUTH_TOKEN 或 ANTHROPIC_API_KEY，无法调用 Claude")


def strip_markdown(text: str) -> str:
    """去掉模型输出的 markdown 代码块包裹"""
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text)
    return text.strip()

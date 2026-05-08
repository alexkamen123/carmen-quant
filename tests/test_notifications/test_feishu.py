# tests/test_notifications/test_feishu.py
import pytest
import respx
import httpx
from finance_agent.notifications.feishu import send_feishu_message


@pytest.mark.asyncio
async def test_send_feishu_success(monkeypatch):
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://open.feishu.cn/hook/test")
    with respx.mock:
        respx.post("https://open.feishu.cn/hook/test").mock(
            return_value=httpx.Response(200, json={"code": 0})
        )
        result = await send_feishu_message("测试消息")
    assert result is True


@pytest.mark.asyncio
async def test_send_feishu_failure_returns_false(monkeypatch):
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://open.feishu.cn/hook/test")
    with respx.mock:
        respx.post("https://open.feishu.cn/hook/test").mock(
            return_value=httpx.Response(500)
        )
        result = await send_feishu_message("测试消息")
    assert result is False

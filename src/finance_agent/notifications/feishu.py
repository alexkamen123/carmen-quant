# src/finance_agent/notifications/feishu.py
import os
import httpx


async def send_feishu_message(text: str) -> bool:
    """
    发送纯文本消息到飞书群机器人。
    飞书机器人设置：群聊 → 设置 → 机器人 → 添加机器人 → 自定义机器人 → 获取 Webhook URL
    """
    webhook_url = os.environ.get("FEISHU_WEBHOOK_URL", "")
    if not webhook_url:
        print("[飞书] FEISHU_WEBHOOK_URL 未设置，跳过推送")
        return False

    payload = {
        "msg_type": "text",
        "content": {"text": text}
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(webhook_url, json=payload)
            response.raise_for_status()
            result = response.json()
            if result.get("code") == 0:
                return True
            print(f"[飞书] 推送失败：{result}")
            return False
    except Exception as e:
        print(f"[飞书] 推送异常：{e}")
        return False

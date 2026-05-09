# src/finance_agent/notifications/feishu.py
import base64
import hashlib
import hmac
import os
import time
import httpx


def _gen_sign(timestamp: int, secret: str) -> str:
    """飞书签名校验：HMAC-SHA256(timestamp\nsecret) → base64"""
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


async def send_feishu_message(text: str) -> bool:
    """
    发送纯文本消息到飞书群机器人（支持签名校验）。
    环境变量：
      FEISHU_WEBHOOK_URL    必填
      FEISHU_WEBHOOK_SECRET 可选，开启签名校验时填入
    """
    webhook_url = os.environ.get("FEISHU_WEBHOOK_URL", "")
    if not webhook_url:
        print("[飞书] FEISHU_WEBHOOK_URL 未设置，跳过推送")
        return False

    payload: dict = {
        "msg_type": "text",
        "content": {"text": text},
    }

    secret = os.environ.get("FEISHU_WEBHOOK_SECRET", "")
    if secret:
        timestamp = int(time.time())
        payload["timestamp"] = str(timestamp)
        payload["sign"] = _gen_sign(timestamp, secret)

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

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


def _build_payload(msg_type: str, content: dict) -> dict:
    """构建带签名的飞书 webhook payload"""
    payload: dict = {"msg_type": msg_type, **content}
    secret = os.environ.get("FEISHU_WEBHOOK_SECRET", "")
    if secret:
        timestamp = int(time.time())
        payload["timestamp"] = str(timestamp)
        payload["sign"] = _gen_sign(timestamp, secret)
    return payload


async def _post(payload: dict) -> bool:
    webhook_url = os.environ.get("FEISHU_WEBHOOK_URL", "")
    if not webhook_url:
        print("[飞书] FEISHU_WEBHOOK_URL 未设置，跳过推送")
        return False
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


async def send_feishu_card(card: dict) -> bool:
    """
    发送卡片消息（interactive）到飞书群机器人。
    card 为完整的卡片 JSON（不含 msg_type）。
    """
    payload = _build_payload("interactive", {"card": card})
    return await _post(payload)


async def send_feishu_message(text: str) -> bool:
    """发送纯文本消息到飞书群机器人（兼容备用）"""
    payload = _build_payload("text", {"content": {"text": text}})
    return await _post(payload)

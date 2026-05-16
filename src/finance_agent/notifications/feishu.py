# src/finance_agent/notifications/feishu.py
import base64
import hashlib
import hmac
import os
import smtplib
import time
from email.mime.text import MIMEText
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


def _send_email_fallback(subject: str, body: str) -> bool:
    """
    飞书推送失败时的邮件兜底。
    需要配置：FALLBACK_EMAIL_TO, SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS。
    未配置则静默跳过。
    """
    to_addr = os.environ.get("FALLBACK_EMAIL_TO", "")
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    if not (to_addr and smtp_user and smtp_pass):
        return False
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "465"))
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = smtp_user
        msg["To"] = to_addr
        if smtp_port == 587:
            with smtplib.SMTP(smtp_host, smtp_port) as server:
                server.starttls()
                server.login(smtp_user, smtp_pass)
                server.send_message(msg)
        else:
            with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
                server.login(smtp_user, smtp_pass)
                server.send_message(msg)
        print(f"[邮件兜底] 已发送至 {to_addr}")
        return True
    except Exception as e:
        print(f"[邮件兜底] 发送失败：{e}")
        return False


async def send_feishu_card(card: dict, fallback_text: str | None = None) -> bool:
    """
    发送卡片消息（interactive）到飞书群机器人。
    card 为完整的卡片 JSON（不含 msg_type）。
    fallback_text：飞书失败时通过邮件发送的纯文本内容（需配置 FALLBACK_EMAIL_TO）。
    """
    payload = _build_payload("interactive", {"card": card})
    ok = await _post(payload)
    if not ok and fallback_text:
        _send_email_fallback("⚠️ 飞书推送失败 | 卡门智投报告", fallback_text)
    return ok


async def send_feishu_message(text: str, fallback_subject: str | None = None) -> bool:
    """发送纯文本消息到飞书群机器人（兼容备用）。失败时发邮件兜底。"""
    payload = _build_payload("text", {"content": {"text": text}})
    ok = await _post(payload)
    if not ok:
        subject = fallback_subject or "⚠️ 飞书推送失败 | 卡门智投"
        _send_email_fallback(subject, text)
    return ok

"""卖出冷却提醒：盯"长期想减、但当下过热"的持仓，等情绪冷却到点了自动推卡。

背景：日报对某些持仓给的是"长期减仓 / 锁利"，但当下单日暴涨 + RSI 超买时不该
追着砍（容易卖在情绪高点回调前）。本模块每个交易日收盘后判一次：
  单日涨幅回到阈值内（默认 ±3%）  且  RSI 退出超买（默认 < 70）
→ 推一张"可以挂单减仓"的卡到飞书。成交前每天最多提醒一次（按日去重）。

先跑通流程版：watch 列表内置在 COOLDOWN_WATCH，条件参数（阈值/RSI 顶）逐项可调。
扩展只需往列表里加一条；不依赖 portfolio.yaml，避免和持仓配置耦合。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pandas as pd
import yfinance as yf

from finance_agent.signals.technical import calculate_signals
from finance_agent.notifications.feishu import send_feishu_card
# 复用 news_monitor 的去重表（news_alerted）与北京时区
from finance_agent.alerts.news_monitor import _load_alerted, _save_alerted, _BJT
from datetime import datetime


# ── 冷却盯盘清单 ──────────────────────────────────────────────────────────
# max_day_change：单日涨跌绝对值回到此值内算"涨势停了"
# rsi_ceiling   ：RSI 退到此值以下算"退出超买"
COOLDOWN_WATCH: list[dict] = [
    {
        "ticker": "DRAM",
        "market": "us",
        "max_day_change": 3.0,
        "rsi_ceiling": 70.0,
        "action": "挂单卖 1 股，留 2 股",
        "reason": "浮盈约 45% 已可观，日报长期建议锁利；现涨势已歇，落袋 1 股不贪最后一口。",
    },
]


def _yf_symbol(ticker: str, market: str) -> str:
    return f"{int(ticker):04d}.HK" if market == "hk" else ticker


def is_cooled(change_pct: float, rsi: float,
              max_day_change: float, rsi_ceiling: float) -> bool:
    """冷却判定（纯函数）：涨势停（单日绝对涨跌回到阈值内）且 RSI 退出超买。"""
    return abs(change_pct) <= max_day_change and rsi < rsi_ceiling


def _evaluate(item: dict) -> dict | None:
    """拉日线、算指标，判断是否已冷却。返回带实测值的 dict，数据缺失返回 None。"""
    sym = _yf_symbol(item["ticker"], item["market"])
    df = yf.download(sym, period="3mo", interval="1d", progress=False, auto_adjust=True)
    if df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0].lower() for c in df.columns]
    else:
        df.columns = [c.lower() for c in df.columns]
    if len(df) < 20:
        return None  # 指标窗口不足（RSI14/MA20 需要足够样本）

    sig = calculate_signals(df, ticker=item["ticker"])
    cooled = is_cooled(sig.change_pct, sig.rsi,
                       item["max_day_change"], item["rsi_ceiling"])
    return {
        **item,
        "change_pct": sig.change_pct,
        "rsi": sig.rsi,
        "close": sig.close,
        "cooled": cooled,
    }


def build_cooldown_card(items: list[dict]) -> dict:
    """构建减仓冷却提醒卡（飞书 v1 interactive，与日报卡同款结构）。"""
    elements: list[dict] = []
    for it in items:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": (
                f"🔔 **{it['ticker']}** 已冷却　现价 {it['close']:.2f}　"
                f"今日 {it['change_pct']:+.1f}%　RSI {it['rsi']:.0f}\n"
                f"📌 计划动作：**{it['action']}**\n"
                f"💡 {it['reason']}"
            )},
        })
    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text",
                      "content": "减仓冷却提醒 · 条件触达即提示，仍需你自行决策；"
                                 "卖完可 finance-agent log-action 记一笔"}],
    })
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "🔔 卡门智投 · 减仓冷却提醒"},
            "template": "orange",
        },
        "elements": elements,
    }


def _fallback_text(items: list[dict]) -> str:
    lines = ["【减仓冷却提醒】"]
    for it in items:
        lines.append(f"{it['ticker']} 已冷却（今日 {it['change_pct']:+.1f}%, "
                     f"RSI {it['rsi']:.0f}）→ {it['action']}")
    return "\n".join(lines)


async def run_cooldown_check(force: bool = False, skip_notify: bool = False) -> int:
    """检查冷却清单，满足条件的推卡。返回推送条数。

    force      : 跳过"是否冷却"判定，强制视为已冷却（仅供跑通流程/测试）。
    skip_notify: 只构建并打印卡，不真正推送飞书。
    """
    today_str = datetime.now(_BJT).strftime("%Y-%m-%d")
    alerted = _load_alerted(today_str)

    loop = asyncio.get_event_loop()
    fired: list[dict] = []
    for item in COOLDOWN_WATCH:
        dedup_key = f"cooldown:{item['ticker']}:{today_str}"
        if dedup_key in alerted:
            print(f"[Cooldown] {item['ticker']} 今日已提醒过，跳过")
            continue

        res = await loop.run_in_executor(None, _evaluate, item)
        if res is None:
            print(f"[Cooldown] {item['ticker']} 数据不足/拉取失败，跳过")
            continue

        is_cooled = res["cooled"] or force
        print(f"[Cooldown] {item['ticker']} 今日 {res['change_pct']:+.1f}% / "
              f"RSI {res['rsi']:.0f} → {'已冷却' if res['cooled'] else '仍过热'}"
              f"{'（force 强推）' if force and not res['cooled'] else ''}")
        if is_cooled:
            res["_dedup_key"] = dedup_key
            fired.append(res)

    if not fired:
        print("[Cooldown] 无标的满足冷却条件，未推送")
        return 0

    card = build_cooldown_card(fired)
    if skip_notify:
        print("[Cooldown] skip_notify：仅打印卡，不推送")
        print(_fallback_text(fired))
    else:
        await send_feishu_card(card, fallback_text=_fallback_text(fired))
        for it in fired:
            _save_alerted(it["_dedup_key"], today_str)

    return len(fired)

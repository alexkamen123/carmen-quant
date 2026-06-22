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
from finance_agent.alerts.news_monitor import (
    _load_alerted, _save_alerted, _BJT, _is_market_open,
)
from datetime import datetime


# ── 冷却盯盘清单 ──────────────────────────────────────────────────────────
# direction     ："spike"   = 涨势冷却（日报想减但当下暴涨超买，等退烧）
#                 "crash"   = 跌势企稳（日报想减但当下暴跌，等止跌再减，别砍在恐慌底）
#                 "rebound" = 借反弹减（深亏仓位反弹逼近 MA20 中期阻力 + 动能停 → 减亏）
# max_day_change：单日涨跌绝对值回到此值内算"动能停了"（三方向通用）
# rsi_gate      ：spike → RSI 退到此值【以下】算退出超买；
#                 crash → RSI 升到此值【以上】算退出超卖；rebound 不看 RSI（置 0）
# ma20_band     ：仅 rebound——现价 ≥ MA20×(1-band) 即算"逼近阻力"（默认 0.04）
COOLDOWN_WATCH: list[dict] = [
    {
        "ticker": "00100",
        "market": "hk",
        "direction": "rebound",
        "max_day_change": 4.0,
        "rsi_gate": 0.0,
        "ma20_band": 0.04,
        "action": "借反弹减 1 股（剩 4 股，逼近 MA20 阻力即减）",
        "reason": "MiniMax 深亏次新股（PS 极高泡沫、纯投机），借反弹减风险；"
                  "价逼近 MA20(~600HKD) 中期阻力、动能停即提醒，别赌它突破回成本。",
    },
]

_LABEL = {"spike": "已冷却", "crash": "已企稳", "rebound": "反弹近阻力"}


def _yf_symbol(ticker: str, market: str) -> str:
    return f"{int(ticker):04d}.HK" if market == "hk" else ticker


def is_settled(change_pct: float, rsi: float, max_day_change: float,
               direction: str, rsi_gate: float) -> bool:
    """情绪平复判定（纯函数）：单日动能停（绝对涨跌≤阈值），且 RSI 退出极值。

    direction="spike" → 退出超买（rsi < rsi_gate）；
    direction="crash" → 退出超卖（rsi > rsi_gate）。
    """
    momentum_stopped = abs(change_pct) <= max_day_change
    rsi_ok = rsi < rsi_gate if direction == "spike" else rsi > rsi_gate
    return momentum_stopped and rsi_ok


def is_rebound_ready(close: float, ma20: float, change_pct: float,
                     max_day_change: float, band: float = 0.04) -> bool:
    """借反弹减判定（纯函数）：深亏仓位反弹逼近 MA20 中期阻力（现价 ≥ MA20×(1-band)）
    且单日动能停（绝对涨跌 ≤ 阈值）→ 提醒借反弹减，别赌它突破回成本。"""
    if ma20 <= 0:
        return False
    near_resistance = close >= ma20 * (1 - band)
    return near_resistance and abs(change_pct) <= max_day_change


def _hk_hist_akshare(ticker: str):
    """港股日线历史 AkShare 兜底（yfinance 对港股新股常空），返回小写 OHLCV df 或 None。"""
    try:
        import akshare as ak
        sym = f"{int(ticker):05d}" if ticker.isdigit() else ticker
        df = ak.stock_hk_daily(symbol=sym, adjust="qfq")
        if df is None or len(df) == 0:
            return None
        df.columns = [str(c).lower() for c in df.columns]
        need = ["open", "high", "low", "close", "volume"]
        if not all(c in df.columns for c in need):
            return None
        return df[need].astype(float).reset_index(drop=True)
    except Exception:
        return None


def _evaluate(item: dict) -> dict | None:
    """拉日线、算指标，判断是否到减仓时机。返回带实测值的 dict，数据缺失返回 None。"""
    sym = _yf_symbol(item["ticker"], item["market"])
    df = yf.download(sym, period="3mo", interval="1d", progress=False, auto_adjust=True)
    if not df.empty:
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0].lower() for c in df.columns]
        else:
            df.columns = [c.lower() for c in df.columns]
    # 港股新股 yfinance 常空/不足 → AkShare 兜底全量历史（已小写 OHLCV）
    if (df is None or df.empty or len(df) < 20) and item["market"] == "hk":
        df = _hk_hist_akshare(item["ticker"])
    if df is None or len(df) < 20:
        return None  # 指标窗口不足（RSI14/MA20 需要足够样本）

    sig = calculate_signals(df, ticker=item["ticker"])
    if item["direction"] == "rebound":
        # 借反弹减：现价逼近 MA20 中期阻力（从下方）且动能停 → 减亏时机
        settled = is_rebound_ready(sig.close, sig.ma20, sig.change_pct,
                                   item["max_day_change"], item.get("ma20_band", 0.04))
    else:
        settled = is_settled(sig.change_pct, sig.rsi, item["max_day_change"],
                             item["direction"], item["rsi_gate"])
    return {
        **item,
        "change_pct": sig.change_pct,
        "rsi": sig.rsi,
        "close": sig.close,
        "ma20": sig.ma20,
        "settled": settled,
    }


def build_cooldown_card(items: list[dict]) -> dict:
    """构建减仓时机提醒卡（飞书 v1 interactive，与日报卡同款结构）。"""
    elements: list[dict] = []
    for it in items:
        label = _LABEL.get(it.get("direction", "spike"), "已平复")
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": (
                f"🔔 **{it['ticker']}** {label}　现价 {it['close']:.2f}　"
                f"今日 {it['change_pct']:+.1f}%　RSI {it['rsi']:.0f}\n"
                f"📌 计划动作：**{it['action']}**\n"
                f"💡 {it['reason']}"
            )},
        })
    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text",
                      "content": "减仓时机提醒 · 条件触达即提示，仍需你自行决策；"
                                 "卖完可 finance-agent log-action 记一笔"}],
    })
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "🔔 卡门智投 · 减仓时机提醒"},
            "template": "orange",
        },
        "elements": elements,
    }


def _fallback_text(items: list[dict]) -> str:
    lines = ["【减仓时机提醒】"]
    for it in items:
        label = _LABEL.get(it.get("direction", "spike"), "已平复")
        lines.append(f"{it['ticker']} {label}（今日 {it['change_pct']:+.1f}%, "
                     f"RSI {it['rsi']:.0f}）→ {it['action']}")
    return "\n".join(lines)


async def run_cooldown_check(force: bool = False, skip_notify: bool = False) -> int:
    """检查冷却清单，满足条件的推卡。返回推送条数。

    force      : 跳过"是否平复"判定，强制视为已平复（仅供跑通流程/测试）。
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

        # 闭市保护：盘中日线 bar 未定型，涨跌/RSI 实时跳动会触发假信号；
        # 本检查应在收盘后跑。force 仅供测试跑通流程时绕过。
        if not force and _is_market_open(item["market"]):
            print(f"[Cooldown] {item['ticker']} {item['market']} 盘中（日线未定型），"
                  f"跳过——本检查应收盘后跑")
            continue

        res = await loop.run_in_executor(None, _evaluate, item)
        if res is None:
            print(f"[Cooldown] {item['ticker']} 数据不足/拉取失败，跳过")
            continue

        settled = res["settled"] or force
        not_yet = {"spike": "仍过热", "crash": "仍在急跌",
                   "rebound": "未到阻力"}.get(item["direction"], "未平复")
        print(f"[Cooldown] {item['ticker']}（{item['direction']}）今日 {res['change_pct']:+.1f}% / "
              f"RSI {res['rsi']:.0f} → {_LABEL[item['direction']] if res['settled'] else not_yet}"
              f"{'（force 强推）' if force and not res['settled'] else ''}")
        if settled:
            res["_dedup_key"] = dedup_key
            fired.append(res)

    if not fired:
        print("[Cooldown] 无标的满足平复条件，未推送")
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

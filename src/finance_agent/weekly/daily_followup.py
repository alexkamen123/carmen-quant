# src/finance_agent/weekly/daily_followup.py
"""
周二到周五的轻量跟进：
  - 读取周一完整报告（data/weekly_latest.json）
  - 拉取推荐品种当前价格，计算周内涨跌幅
  - 重跑技术初筛，判断机会是否还在
  - Claude 给出 2-3 句跟进意见
  - 发飞书文本消息（不发全卡片，避免打扰）
"""
import json
from datetime import date
from pathlib import Path

import yfinance as yf
import pandas as pd

from finance_agent.agents.claude_client import claude_cli_chat, has_claude_cli
from finance_agent.agents.bull_agent import deepseek_chat
from finance_agent.weekly.allocation_advisor import CANDIDATE_POOL, _quick_screen
import asyncio


FOLLOWUP_SYSTEM = """你是家庭投资组合的每日跟进助手。
基于本周周一的配置建议，结合今日的价格变动，给出简短跟进意见。

要求：
- 纯文字，2-4 句话，不超过 100 字
- 说明推荐品种本周表现（涨了/跌了多少）
- 机会是否还在/是否已错过
- 是否有新增超卖机会需要关注
- 语气轻松，像朋友提醒而非正式报告"""

FOLLOWUP_USER = """周一（{report_date}）的配置建议摘要：
{recommendation_summary}

今日各推荐品种表现：
{price_changes}

今日技术初筛新增机会：{new_opportunities}

请给出 2-4 句跟进意见。"""


def _fetch_price_changes(tickers: list[str]) -> list[dict]:
    """拉取本周一到今日的价格变化。"""
    changes = []
    for ticker in tickers:
        try:
            yf_ticker = f"{int(ticker):04d}.HK" if ticker.isdigit() else ticker
            df = yf.download(yf_ticker, period="7d", progress=False, auto_adjust=True)
            if df.empty or len(df) < 2:
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0].lower() for c in df.columns]
            else:
                df.columns = [c.lower() for c in df.columns]
            close = df["close"].dropna()
            price_start = float(close.iloc[0])
            price_now = float(close.iloc[-1])
            pct = (price_now / price_start - 1) * 100
            changes.append({
                "ticker": ticker,
                "price_start": round(price_start, 2),
                "price_now": round(price_now, 2),
                "pct_change": round(pct, 1),
            })
        except Exception as e:
            print(f"[Followup] {ticker} 价格拉取失败: {e}")
    return changes


async def run_daily_followup() -> str | None:
    """
    执行每日跟进逻辑。
    返回要推送到飞书的文字内容，或 None（若无周报数据则跳过）。
    """
    weekly_path = Path("data/weekly_latest.json")
    if not weekly_path.exists():
        print("[Followup] 未找到 weekly_latest.json，跳过今日跟进")
        return None

    with open(weekly_path) as f:
        weekly = json.load(f)

    report_date = weekly.get("date", "本周一")
    watch_tickers = weekly.get("watch_tickers", [])
    diagnosis = weekly.get("diagnosis", {})
    hedge_instruments = weekly.get("hedge_instruments", [])
    opportunities = weekly.get("opportunities", [])

    # ── 推荐品种价格变化 ──
    loop = asyncio.get_event_loop()
    price_changes = await loop.run_in_executor(None, _fetch_price_changes, watch_tickers)

    if not price_changes:
        price_change_str = "（无法获取价格数据）"
    else:
        price_change_str = "\n".join(
            f"  {c['ticker']}：{c['price_start']} → {c['price_now']} "
            f"（{'+' if c['pct_change'] >= 0 else ''}{c['pct_change']}%）"
            for c in sorted(price_changes, key=lambda x: x["pct_change"])
        )

    # ── 今日快速初筛，看是否有新机会 ──
    all_new: list[dict] = []
    for category, tickers in CANDIDATE_POOL.items():
        market = "hk" if any(t.isdigit() for t in tickers) else "us"
        screened = await loop.run_in_executor(None, _quick_screen, tickers, market)
        all_new.extend(screened)

    # 已经在 watch list 里的不算新增
    existing_tickers = set(watch_tickers)
    new_opps = [c for c in all_new if c["ticker"] not in existing_tickers]
    new_opp_str = (
        "、".join(f"{c['ticker']}（RSI={c['rsi']}）" for c in new_opps[:3])
        if new_opps else "暂无新增超卖机会"
    )

    # ── 推荐摘要 ──
    hedge_summary_parts = []
    for block in hedge_instruments:
        names = [ins["ticker"] for ins in block.get("instruments", [])]
        hedge_summary_parts.append(f"{block['direction']}→{'/'.join(names)}")
    opp_tickers = [op["ticker"] for op in opportunities]
    rec_summary = (
        f"对冲方向：{' | '.join(hedge_summary_parts)}\n"
        f"机会标的：{', '.join(opp_tickers) or '无'}"
    )

    # ── Claude 跟进意见 ──
    user_msg = FOLLOWUP_USER.format(
        report_date=report_date,
        recommendation_summary=rec_summary,
        price_changes=price_change_str,
        new_opportunities=new_opp_str,
    )
    try:
        if has_claude_cli():
            comment = await claude_cli_chat(FOLLOWUP_SYSTEM, user_msg, timeout=60)
        else:
            raise RuntimeError("无 Claude CLI")
    except Exception as e:
        print(f"[Followup] Claude 失败，降级 DeepSeek: {e}")
        comment = await deepseek_chat(FOLLOWUP_SYSTEM, user_msg)

    today = date.today().strftime("%m/%d")
    output = (
        f"📌 卡门智投 · {today} 配置跟进\n\n"
        f"{comment.strip()}\n\n"
        f"价格变化：\n{price_change_str}\n"
        f"新增超卖：{new_opp_str}"
    )
    return output
